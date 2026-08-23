"""P1-01/02/03：schema 2.0 校验、稳定 chunk ID、SearchResult 元数据测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from app.rag_eval import dataset_schema as ds
from app.services.knowledge import SearchResult, parse_stable_chunk_key, stable_chunk_key


def _write(dataset: dict) -> Path:
    fd, path = tempfile.mkstemp(suffix=".json")
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(dataset, fh, ensure_ascii=False)
    return Path(path)


def _valid_case(**overrides):
    case = {
        "id": "c1",
        "domain": "SERVICE",
        "risk": "LOW",
        "question": "q",
        "referenceContextIds": ["SERVICE:llm-gateway:1:0"],
        "provenance": {"sourceFile": "a.md", "reviewStatus": "approved"},
    }
    case.update(overrides)
    return case


class StableChunkKeyTests(unittest.TestCase):
    def test_roundtrip(self):
        key = stable_chunk_key("SERVICE", "llm-gateway", 3, 2)
        self.assertEqual(key, "SERVICE:llm-gateway:3:2")
        self.assertEqual(parse_stable_chunk_key(key), ("SERVICE", "llm-gateway", 3, 2))

    def test_enum_domain(self):
        from app.core.enums import KnowledgeDomain

        key = stable_chunk_key(KnowledgeDomain.COMPLIANCE, "gift", 1, 0)
        self.assertEqual(key, "COMPLIANCE:gift:1:0")

    def test_missing_fields(self):
        # domain=None 时空串产生前导冒号
        self.assertEqual(stable_chunk_key(None, "k", None, None), ":k::")


class SearchResultMetadataTests(unittest.TestCase):
    def test_stable_key_property(self):
        r = SearchResult(1, "a.md", "content", 0.9, source_key="k", version=2, source_index=3, domain="SERVICE")
        self.assertEqual(r.stable_key, "SERVICE:k:2:3")

    def test_defaults_none(self):
        r = SearchResult(1, "a.md", "content", 0.9)
        self.assertIsNone(r.source_key)
        # source_key/version/index/domain 均为 None → 全空占位
        self.assertEqual(r.stable_key, ":::")


class SchemaV2Tests(unittest.TestCase):
    def test_valid_v2_case(self):
        dataset = {"schemaVersion": "2.0", "cases": [_valid_case()]}
        version, cases = ds.load_dataset(_write(dataset), "rag")
        self.assertEqual(version, "2.0")
        self.assertEqual(len(cases), 1)

    def test_missing_domain_rejected(self):
        dataset = {"schemaVersion": "2.0", "cases": [_valid_case(domain=None)]}
        with self.assertRaises(ds.DatasetValidationError):
            ds.load_dataset(_write(dataset), "rag")

    def test_cross_domain_reference_rejected(self):
        case = _valid_case(referenceContextIds=["COMPLIANCE:other:1:0"])
        with self.assertRaises(ds.DatasetValidationError):
            ds.load_dataset(_write({"schemaVersion": "2.0", "cases": [case]}), "rag")

    def test_bad_chunk_key_format_rejected(self):
        case = _valid_case(referenceContextIds=["SERVICE:noseparator"])
        with self.assertRaises(ds.DatasetValidationError):
            ds.load_dataset(_write({"schemaVersion": "2.0", "cases": [case]}), "rag")

    def test_critical_requires_claims_or_behaviors(self):
        case = _valid_case(suite="critical")
        with self.assertRaises(ds.DatasetValidationError):
            ds.load_dataset(_write({"schemaVersion": "2.0", "cases": [case]}), "rag")

    def test_critical_with_forbidden_claims_ok(self):
        case = _valid_case(suite="critical", forbiddenClaims=["不得承诺退款"])
        ds.load_dataset(_write({"schemaVersion": "2.0", "cases": [case]}), "rag")

    def test_duplicate_id_rejected(self):
        dataset = {"schemaVersion": "2.0", "cases": [_valid_case(), _valid_case(id="c1")]}
        with self.assertRaises(ds.DatasetValidationError):
            ds.load_dataset(_write(dataset), "rag")

    def test_legacy_v1_still_compatible(self):
        legacy = [
            {"id": "a", "question": "q", "expectedSources": ["x.md"], "expectedTerms": ["HIGH"]}
        ]
        version, cases = ds.load_dataset(_write(legacy), "rag")
        self.assertEqual(version, "1.0")
        self.assertEqual(len(cases), 1)


if __name__ == "__main__":
    unittest.main()