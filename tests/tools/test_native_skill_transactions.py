"""Native learning regression tests. Real processes, isolated skill roots, no LLM."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SKILL = "---\nname: probe\ndescription: Use when testing native learning. Keep edits.\n---\nAlpha original.\nBeta original.\n"


def wait_file(path, seconds=20):
    deadline = time.monotonic() + seconds
    while not Path(path).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {path}")
        time.sleep(0.01)


def worker(spec):
    from tools import skill_manager_tool as smt
    from tools import skills_tool
    if spec.get("reuse"):
        result = {"list": json.loads(skills_tool.skills_list()),
                  "view": json.loads(skills_tool.skill_view("probe", preprocess=False))}
    else:
        original = smt.atomic_write_text
        def slow_write(*args, **kwargs):
            # Broaden the read-modify-write race. With the transaction lock both
            # independent patches survive; atomic file replacement alone loses one.
            time.sleep(spec.get("delay", 0))
            result = original(*args, **kwargs)
            if spec.get("paused"):
                Path(spec["paused"]).touch()
                wait_file(spec["release"])
            return result
        smt.atomic_write_text = slow_write
        if spec.get("ready"):
            Path(spec["ready"]).touch()
            wait_file(spec["start"])
        result = json.loads(smt.skill_manage(**spec["call"]))
    Path(spec["result"]).write_text(json.dumps(result))


class NativeSkillTransactionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        (self.home / "skills").mkdir(parents=True)
        self.env = {**os.environ, "HERMES_HOME": str(self.home),
                    "HERMES_SKILL_WRITE_SCOPE": "local", "HERMES_SKILL_LOCK_TIMEOUT": "10",
                    "HERMES_YOLO_MODE": "0"}
        self.env.pop("HERMES_CONFIG_PATH", None)
        self.env.pop("HERMES_WRITE_SAFE_ROOT", None)
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        from agent import skill_utils
        skill_utils._raw_config_cache_clear()
        skill_utils._external_dirs_cache_clear()
        self.addCleanup(skill_utils._raw_config_cache_clear)
        self.addCleanup(skill_utils._external_dirs_cache_clear)
        from tools import skill_manager_tool as smt, skills_tool
        self.smt = smt
        self.addCleanup(patch.stopall)
        patch.object(smt, "SKILLS_DIR", self.home / "skills").start()
        patch.object(skills_tool, "SKILLS_DIR", self.home / "skills").start()
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)

    def config(self, content):
        (self.home / "config.yaml").write_text(content)
        from agent import skill_utils
        skill_utils._raw_config_cache_clear()
        skill_utils._external_dirs_cache_clear()

    def call(self, **kwargs):
        return json.loads(self.smt.skill_manage(action="", name="", **kwargs))

    def seed(self):
        result = self.call(operations=[{"action": "create", "name": "probe", "content": SKILL}])
        self.assertTrue(result["success"], result)
        return self.home / "skills/probe/SKILL.md"

    def launch(self, label, call=None, env=None, **spec):
        result_path = Path(self.temp.name) / f"{label}.json"
        spec.update(result=str(result_path), call=call)
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(spec)],
                                cwd=ROOT, env=env or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(proc)
        return proc, result_path

    def finish(self, launched):
        proc, path = launched
        out, err = proc.communicate(timeout=40)
        self.assertEqual(proc.returncode, 0, out + err)
        return json.loads(path.read_text())

    def race(self, calls, envs=None):
        start = Path(self.temp.name) / "start"
        runs = []
        for i, call in enumerate(calls):
            ready = Path(self.temp.name) / f"ready-{i}"
            runs.append(self.launch(f"race-{i}", call, env=(envs[i] if envs else None),
                                    delay=0.2, ready=str(ready), start=str(start)))
        for i in range(len(calls)):
            wait_file(Path(self.temp.name) / f"ready-{i}")
        start.touch()
        return [self.finish(run) for run in runs]

    def test_same_skill_create_race_has_one_winner(self):
        calls = [{"action": "create", "name": "probe", "content": SKILL + f"Worker {i}.\n"}
                 for i in range(4)]
        results = self.race(calls)
        self.assertEqual(sum(r["success"] for r in results), 1, results)
        winner = next(i for i, r in enumerate(results) if r["success"])
        self.assertEqual((self.home / "skills/probe/SKILL.md").read_text(), calls[winner]["content"])

    def test_same_skill_batch_create_race_does_not_remove_winner(self):
        calls = [{"action": "", "name": "", "operations": [
            {"action": "create", "name": "probe", "content": SKILL + f"Winner {i}.\n"},
            {"action": "write_file", "name": "probe", "file_path": "references/note.md", "file_content": str(i)}]}
            for i in range(3)]
        results = self.race(calls)
        self.assertEqual(sum(r["success"] for r in results), 1, results)
        winner = next(i for i, r in enumerate(results) if r["success"])
        self.assertIn(f"Winner {winner}.", (self.home / "skills/probe/SKILL.md").read_text())
        self.assertEqual((self.home / "skills/probe/references/note.md").read_text(), str(winner))

    def test_independent_process_patches_both_survive(self):
        target = self.seed()
        results = self.race([
            {"action": "patch", "name": "probe", "old_string": "Alpha original.", "new_string": "Alpha changed."},
            {"action": "patch", "name": "probe", "old_string": "Beta original.", "new_string": "Beta changed."}])
        self.assertTrue(all(r["success"] for r in results), results)
        self.assertIn("Alpha changed.", target.read_text())
        self.assertIn("Beta changed.", target.read_text())

    def test_shared_create_dir_across_profiles_uses_same_lock(self):
        shared = Path(self.temp.name) / "shared"
        homes = [Path(self.temp.name) / f"profile-{i}" for i in range(2)]
        envs = []
        for home in homes:
            home.mkdir()
            (home / "config.yaml").write_text(f"skills:\n  create_dir: {shared}\n")
            envs.append({**self.env, "HERMES_HOME": str(home), "HERMES_SKILL_WRITE_SCOPE": "", "TMPDIR": str(home)})
        calls = [{"action": "create", "name": "probe", "content": SKILL} for _ in homes]
        results = self.race(calls, envs)
        self.assertEqual(sum(r["success"] for r in results), 1, results)
        self.assertEqual((shared / "probe/SKILL.md").read_text(), SKILL)

    def test_threads_patch_without_lost_updates(self):
        from concurrent.futures import ThreadPoolExecutor
        target = self.seed()
        original = self.smt.atomic_write_text
        def slow_write(*args, **kwargs):
            time.sleep(0.1)
            return original(*args, **kwargs)
        def edit(label):
            return self.call(operations=[{"name": "probe", "action": "patch",
                "old_string": f"{label} original.", "new_string": f"{label} threaded."}])
        with patch.object(self.smt, "atomic_write_text", side_effect=slow_write), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(edit, ["Alpha", "Beta"]))
        self.assertTrue(all(r["success"] for r in results), results)
        self.assertIn("Alpha threaded.", target.read_text())
        self.assertIn("Beta threaded.", target.read_text())

    def test_failed_batch_rollback_cannot_overwrite_waiting_worker(self):
        target = self.seed()
        paused = Path(self.temp.name) / "paused"
        release = Path(self.temp.name) / "release"
        first = self.launch("batch", {"action": "", "name": "", "operations": [
            {"name": "probe", "action": "patch", "old_string": "Alpha original.", "new_string": "Alpha transient."},
            {"name": "probe", "action": "write_file", "file_path": "bad/path", "file_content": "fail"}]},
            paused=str(paused), release=str(release))
        wait_file(paused)
        ready = Path(self.temp.name) / "second-ready"
        start = Path(self.temp.name) / "second-start"
        second = self.launch("other", {"action": "patch", "name": "probe", "old_string": "Beta original.", "new_string": "Beta durable."},
                             ready=str(ready), start=str(start))
        wait_file(ready)
        start.touch()
        time.sleep(0.4)
        self.assertFalse(second[1].exists(), "other process entered an uncommitted transaction")
        release.touch()
        self.assertFalse(self.finish(first)["success"])
        self.assertTrue(self.finish(second)["success"])
        self.assertIn("Alpha original.", target.read_text())
        self.assertIn("Beta durable.", target.read_text())
        self.assertNotIn("transient", target.read_text())

    def test_lock_timeout_is_bounded_and_does_not_mutate(self):
        target = self.seed()
        paused, release = Path(self.temp.name) / "paused", Path(self.temp.name) / "release"
        first = self.launch("holding", {"action": "patch", "name": "probe", "old_string": "Alpha original.", "new_string": "Alpha held."},
                            paused=str(paused), release=str(release))
        wait_file(paused)
        other = self.launch("timeout", {"action": "patch", "name": "probe", "old_string": "Beta original.", "new_string": "Beta lost."},
                            env={**self.env, "HERMES_SKILL_LOCK_TIMEOUT": "0.15"})
        result = self.finish(other)
        self.assertFalse(result["success"], result)
        self.assertIn("Timed out", result["error"])
        release.touch()
        self.assertTrue(self.finish(first)["success"])
        self.assertIn("Beta original.", target.read_text())

    def test_normal_learning_immediately_reusable_in_new_process(self):
        self.seed()
        from tools import write_approval as wa
        self.assertTrue(wa.evaluate_gate(wa.SKILLS).allow)
        self.assertEqual(wa.pending_count(wa.SKILLS), 0)
        result = self.finish(self.launch("reuse", reuse=True))
        self.assertIn("probe", json.dumps(result["list"]))
        self.assertTrue(result["view"]["success"], result)
        self.assertEqual(result["view"]["content"], SKILL)

    def test_explicit_approval_stages_without_reuse_until_approved(self):
        self.config("skills:\n  write_approval: true\n")
        result = self.call(operations=[{"name": "probe", "action": "create", "content": SKILL}])
        self.assertTrue(result["staged"], result)
        self.assertFalse((self.home / "skills/probe").exists())
        from tools import write_approval as wa
        self.assertEqual(wa.pending_count(wa.SKILLS), 1)
        pending = wa.get_pending(wa.SKILLS, result["pending_id"])
        assert pending is not None
        approved = json.loads(self.smt.apply_skill_pending(pending["payload"]))
        self.assertTrue(approved["success"], approved)
        self.assertEqual((self.home / "skills/probe/SKILL.md").read_text(), SKILL)

    def test_disabled_readonly_and_invalid_scope_block_even_approved_replay(self):
        target = self.seed()
        self.config("skills:\n  write_approval: true\n")
        payload = {"name": "probe", "action": "patch", "old_string": "Alpha original.", "new_string": "Private review."}
        from tools import write_approval as wa, skills_tool
        for scope in ("deny", "readonly", "invalid"):
            with self.subTest(scope=scope), patch.dict(os.environ, {"HERMES_SKILL_WRITE_SCOPE": scope}):
                result = self.call(operations=[payload])
                self.assertFalse(result["success"], result)
                result = json.loads(self.smt.apply_skill_pending(payload))
                self.assertFalse(result["success"], result)
                self.assertEqual(wa.pending_count(wa.SKILLS), 0)
                self.assertTrue(json.loads(skills_tool.skill_view("probe", preprocess=False))["success"])
        self.assertEqual(target.read_text(), SKILL)

    def test_owned_external_bundled_hub_and_symlink_guards(self):
        target = self.seed()
        payload = {"name": "probe", "action": "patch", "old_string": "Alpha original.", "new_string": "Unauthorized."}
        from tools import skill_usage
        for predicate in ("is_bundled", "is_hub_installed", "is_protected_builtin"):
            with self.subTest(predicate=predicate), patch.object(skill_usage, predicate, return_value=True):
                self.assertFalse(self.call(operations=[payload])["success"])
        outside = Path(self.temp.name) / "canonical"
        outside.mkdir()
        (outside / "probe").mkdir()
        (outside / "probe/SKILL.md").write_text(SKILL)
        with patch.object(self.smt, "_find_skill", return_value={"path": outside / "probe"}):
            self.assertFalse(self.call(operations=[payload])["success"])
        (target.parent / "references").symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.call(operations=[payload])["success"])
        self.assertEqual(target.read_text(), SKILL)
        self.assertEqual((outside / "probe/SKILL.md").read_text(), SKILL)

    def test_background_review_ownership_still_blocks_local_user_skill(self):
        target = self.seed()
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin
        token = set_current_write_origin("background_review")
        try:
            result = self.call(operations=[{"name": "probe", "action": "patch", "old_string": "Alpha original.", "new_string": "No."}])
            self.assertFalse(result["success"], result)
            self.assertIn("not curator-managed", result["error"])
        finally:
            reset_current_write_origin(token)
        self.assertEqual(target.read_text(), SKILL)

    def test_real_bundled_manifest_blocks_categorized_alias(self):
        target = self.seed()
        category = self.home / "skills/category"
        category.mkdir()
        target.parent.rename(category / "probe")
        (self.home / "skills/.bundled_manifest").write_text("probe:test-hash\n")
        for name in ("probe", "category/probe"):
            with self.subTest(name=name):
                result = self.call(operations=[{"name": name, "action": "patch", "old_string": "Alpha original.", "new_string": "No."}])
                self.assertFalse(result["success"], result)
                self.assertIn("bundled", result["error"])
        self.assertEqual((category / "probe/SKILL.md").read_text(), SKILL)

    def test_local_scope_refuses_external_create_dir_before_staging(self):
        outside = Path(self.temp.name) / "external-owner"
        self.config(f"skills:\n  create_dir: {outside}\n  write_approval: true\n")
        result = self.call(operations=[{"name": "probe", "action": "create", "content": SKILL}])
        self.assertFalse(result["success"], result)
        self.assertFalse(outside.exists())
        self.assertFalse((self.home / "pending").exists())

    def test_configured_scanner_blocks_and_restores(self):
        target = self.seed()
        self.config("skills:\n  guard_agent_created: true\n")
        with patch.object(self.smt, "scan_skill") as scan, \
             patch.object(self.smt, "should_allow_install", return_value=(False, "test finding")), \
             patch.object(self.smt, "format_scan_report", return_value="blocked"):
            result = self.call(operations=[{"name": "probe", "action": "patch", "old_string": "Alpha original.", "new_string": "Scanner test."}])
            self.assertFalse(result["success"], result)
            self.assertIn("Security scan blocked", result["error"])
            scan.assert_called_once()
        self.assertEqual(target.read_text(), SKILL)

    def test_unexpected_batch_exception_rolls_back_without_success_side_effects(self):
        target = self.seed()
        with patch.dict(self.smt._ACTION_HANDLERS, {"write_file": lambda _: (_ for _ in ()).throw(OSError("injected disk failure"))}), \
             patch.object(self.smt, "_record_success") as record:
            result = self.call(operations=[
                {"name": "probe", "action": "patch", "old_string": "Alpha original.", "new_string": "Transient."},
                {"name": "probe", "action": "write_file", "file_path": "references/a.md", "file_content": "x"}])
            self.assertFalse(result["success"], result)
            self.assertIn("injected disk failure", result["error"])
            record.assert_not_called()
        self.assertEqual(target.read_text(), SKILL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(json.loads(sys.argv[2]))
    else:
        unittest.main()
