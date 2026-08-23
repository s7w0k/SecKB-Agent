"""P1-04/P1-08：legacy 迁移脚本与 validate 命令测试。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.validate import sha256, validate_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_migrate_module():
    path = PROJECT_ROOT / "scripts" / "migrate_legacy_to_v2.py"
    spec = importlib.util.spec_from_file_location("migrate_legacy_to_v2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_dataset(dataset: dict) -> Path:
    fd, path = tempfile.mkstemp(suffix=".json")
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(dataset, fh, ensure_ascii=False)
    return Path(path)


def _v2_case(**overrides):
    case = {
        "id": "c1",
        "domain": "SERVICE",
        "risk": "LOW",
        "question": "q",
        "referenceContextIds": ["SERVICE:llm-gateway:1:0"],
        "provenance": {"sourceFile": "a.md", "reviewStatus": "pending"},
    }
    case.update(overrides)
    return case


class ValidateFileTests(unittest.TestCase):
    def test_ok_case_records_checksum(self):
        path = _write_dataset({"schemaVersion": "2.0", "cases": [_v2_case()]})
        result = validate_file(path, available={"SERVICE:llm-gateway:1:0"})
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["checksum"], sha256(path))
        self.assertEqual(result["schemaVersion"], "2.0")
        self.assertEqual(result["caseCount"], 1)

    def test_missing_file(self):
        result = validate_file(Path("no/such/file.json"))
        self.assertFalse(result["ok"])
        self.assertIn("file not found", result["errors"])

    def test_illegal_risk_fails(self):
        path = _write_dataset({"schemaVersion": "2.0", "cases": [_v2_case(risk="HOME")]})
        result = validate_file(path)
        self.assertFalse(result["ok"])
        self.assertTrue(any("risk" in err for err in result["errors"]), result["errors"])

    def test_duplicate_id_fails(self):
        path = _write_dataset({"schemaVersion": "2.0", "cases": [_v2_case(), _v2_case(id="c1")]})
        result = validate_file(path)
        self.assertFalse(result["ok"])
        self.assertTrue(any("重复" in err for err in result["errors"]), result["errors"])

    def test_cross_domain_reference_fails(self):
        case = _v2_case(referenceContextIds=["COMPLIANCE:other:1:0"])
        path = _write_dataset({"schemaVersion": "2.0", "cases": [case]})
        result = validate_file(path)
        self.assertFalse(result["ok"])
        self.assertTrue(any("跨域" in err for err in result["errors"]), result["errors"])

    def test_missing_chunk_in_available_set_fails(self):
        path = _write_dataset({"schemaVersion": "2.0", "cases": [_v2_case()]})
        result = validate_file(path, available=set())
        self.assertFalse(result["ok"])
        self.assertTrue(any("不存在" in err for err in result["errors"]), result["errors"])

    def test_skipped_db_check_passes_when_refs_format_ok(self):
        path = _write_dataset({"schemaVersion": "2.0", "cases": [_v2_case()]})
        result = validate_file(path, available=None)
        self.assertTrue(result["ok"], result["errors"])


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_migrate_module()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migrate_case_marks_mental_legacy_and_preserves_fields(self):
        src = {"id": "old-1", "question": "q", "expectedSources": ["a.md"]}
        migrated = self.module.migrate_case(src, 0)
        self.assertEqual(migrated["id"], "old-1")
        self.assertEqual(migrated["domain"], "MENTAL")
        self.assertEqual(migrated["scenario"], "legacy")
        self.assertTrue(migrated["legacy"])
        self.assertEqual(migrated["question"], "q")
        self.assertEqual(migrated["expectedSources"], ["a.md"])
        self.assertEqual(migrated["provenance"]["reviewStatus"], "legacy")

    def test_migrate_case_generates_id_when_missing(self):
        migrated = self.module.migrate_case({"question": "q"}, 7)
        self.assertEqual(migrated["id"], "legacy-7")

    def test_migrate_writes_dst_and_report_without_touching_src(self):
        src_path = self.tmp / "legacy.json"
        dst_path = self.tmp / "out" / "mental-legacy.json"
        report_path = self.tmp / "out" / "report.json"
        src_data = [
            {"id": "a", "question": "q1", "expectedSources": ["x.md"]},
            {"id": "b", "question": "q2", "expectedSources": ["y.md"]},
        ]
        src_path.write_text(json.dumps(src_data), encoding="utf-8")

        report = self.module.migrate(src_path, dst_path, report_path)

        self.assertEqual(report["migratedCount"], 2)
        self.assertEqual(report["domains"], {"MENTAL": 2})
        self.assertEqual(len(report["mapping"]), 2)
        # 原文件未被覆盖
        self.assertEqual(json.loads(src_path.read_text(encoding="utf-8")), src_data)
        # 迁移结果每一条均显式标记
        migrated = json.loads(dst_path.read_text(encoding="utf-8"))
        for case in migrated:
            self.assertEqual(case["domain"], "MENTAL")
            self.assertEqual(case["scenario"], "legacy")
            self.assertTrue(case["legacy"])
        # 报告可读
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["migratedCount"], 2)

    def test_migrate_report_source_sha256_matches_input(self):
        src_path = self.tmp / "legacy.json"
        src_path.write_text(json.dumps([{"id": "a", "question": "q"}]), encoding="utf-8")
        report = self.module.migrate(src_path, self.tmp / "out.json", self.tmp / "r.json")
        self.assertEqual(report["sourceSha256"], sha256(src_path))


if __name__ == "__main__":
    unittest.main()
