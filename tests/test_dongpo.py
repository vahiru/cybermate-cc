import hashlib
import json
import math
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from cgm import config, dongpo


def model_fixture(**overrides):
    return {
        "format": "dongpo-judge-char-tfidf-lr", "format_version": 1,
        "features": ["不", "😀", "a b"], "idf": [1, 2, 1],
        "coefficients": [2, -1, 3], "intercept": 0, "threshold": 0.5,
        "ngram_range": [1, 3], "lowercase": True, "sublinear_tf": True,
        **overrides,
    }


class DongpoInferenceTests(unittest.TestCase):
    def test_unicode_case_whitespace_and_unknown_features(self):
        judge = dongpo.DongpoJudge(model_fixture())
        for text, decision in (("不", 2), ("😀", -1), ("A B", 3),
                               ("A\n\tB", 3), ("A\nB", 0), ("词表外", 0)):
            with self.subTest(text=text):
                self.assertAlmostEqual(judge.score(text), 1 / (1 + math.exp(-decision)), 14)
        for text in ("", "\u0085 \t", None):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "非空文本"):
                judge.score(text)

    def test_sublinear_tf_idf_and_l2_normalization(self):
        first, second = 1 + math.log(2), 2
        decision = (first * 2 - second) / math.sqrt(first ** 2 + second ** 2)
        judge = dongpo.DongpoJudge(model_fixture())
        self.assertAlmostEqual(judge.score("不不😀"), 1 / (1 + math.exp(-decision)), 14)
        judge = dongpo.DongpoJudge(model_fixture(sublinear_tf=False))
        self.assertAlmostEqual(judge.score("不不😀"), 1 / (1 + math.exp(-math.sqrt(0.5))), 14)

    def test_model_options_and_stable_sigmoid(self):
        judge = dongpo.DongpoJudge(model_fixture(lowercase=False))
        self.assertEqual(judge.score("A B"), 0.5)
        self.assertEqual(dongpo.DongpoJudge(model_fixture(intercept=1000)).score("词表外"), 1)
        self.assertEqual(dongpo.DongpoJudge(model_fixture(intercept=-1000)).score("词表外"), 0)

    def test_unsupported_models_are_rejected(self):
        for overrides in ({"format_version": 2}, {"format": "other"},
                          {"idf": []}, {"coefficients": []}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                dongpo.DongpoJudge(model_fixture(**overrides))

    def test_keyword_gate_does_not_load_model_for_plain_text(self):
        with patch.object(dongpo, "load_judge") as load:
            for text in ("服务已经恢复了", "", " \n"):
                self.assertFalse(dongpo.detect(text))
            load.assert_not_called()

    def test_all_upstream_keywords_trigger_scoring(self):
        judge = MagicMock(threshold=0.5)
        judge.score.return_value = 0.9
        with patch.object(dongpo, "load_judge", return_value=judge):
            for keyword in ("不", "但", "仅", "凭", "只", "尚", "却", "无法", "更准确"):
                with self.subTest(keyword=keyword):
                    self.assertTrue(dongpo.detect(keyword))
                    judge.score.assert_called_with(keyword)

    def test_unicode_tail_strict_threshold_and_blank_tail(self):
        judge = MagicMock(threshold=0.5)
        judge.score.return_value = 0.5
        with patch.object(dongpo, "load_judge", return_value=judge):
            self.assertFalse(dongpo.detect("不" + "😀" * 120))
            judge.score.assert_called_once_with("😀" * 120)
            judge.score.return_value = 0.50001
            self.assertTrue(dongpo.detect("不" + "😀" * 119))
            judge.score.assert_called_with("不" + "😀" * 119)
            judge.score.reset_mock()
            self.assertFalse(dongpo.detect("不" + " " * 120))
            judge.score.assert_not_called()


class DongpoModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "model.json"
        self.data = json.dumps(model_fixture(), ensure_ascii=False).encode()
        blob = b"blob %d\0" % len(self.data) + self.data
        patches = (
            patch.object(dongpo, "MODEL_PATH", self.path),
            patch.object(dongpo, "MODEL_SIZE", len(self.data)),
            patch.object(dongpo, "MODEL_BLOB", hashlib.sha1(blob).hexdigest()),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        dongpo._load_judge.cache_clear()
        self.addCleanup(dongpo._load_judge.cache_clear)

    def test_exact_bytes_are_required(self):
        self.assertIsInstance(dongpo._validate_model(self.data), dongpo.DongpoJudge)
        for data in (self.data + b" ", self.data.replace(b"true", b"null", 1)):
            with self.subTest(data=data[:30]), self.assertRaisesRegex(ValueError, "完整性"):
                dongpo._validate_model(data)

    def test_one_lazy_load_shared_by_threads_and_later_calls(self):
        self.path.write_bytes(self.data)
        validate = dongpo._validate_model
        with patch.object(dongpo, "_validate_model", wraps=validate) as validation:
            with ThreadPoolExecutor(max_workers=8) as executor:
                judges = list(executor.map(lambda _: dongpo.load_judge(), range(20)))
            self.assertTrue(all(judge is judges[0] for judge in judges))
            self.assertIs(dongpo.load_judge(), judges[0])
            validation.assert_called_once_with(self.data)

    def test_missing_model_has_setup_instructions(self):
        with self.assertRaisesRegex(RuntimeError, "python -m cgm.dongpo --download"):
            dongpo.load_judge()

    def test_download_validates_before_replacing_and_reuses_valid_file(self):
        self.path.write_bytes(b"old invalid model")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"truncated"
        with patch.object(dongpo.urllib.request, "urlopen", return_value=response) as request:
            with self.assertRaisesRegex(ValueError, "完整性"):
                dongpo.download_model()
            self.assertEqual(self.path.read_bytes(), b"old invalid model")
            response.__enter__.return_value.read.return_value = self.data
            dongpo.download_model()
            self.assertEqual(self.path.read_bytes(), self.data)
            self.assertFalse(self.path.with_suffix(".json.tmp").exists())
            request.assert_called_with(dongpo.MODEL_URL, timeout=120)
            request.reset_mock()
            dongpo.download_model()
            request.assert_not_called()


class DongpoConfigTests(unittest.TestCase):
    def test_default_and_disabled_values(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(config.load(require_token=False, require_llm=False).not_dongpo)
            for value in ("0", "false", "OFF", " no "):
                with self.subTest(value=value), patch.dict("os.environ", {"CGM_NOT_DONGPO": value}):
                    self.assertFalse(config.load(require_token=False, require_llm=False).not_dongpo)


@unittest.skipUnless(dongpo.MODEL_PATH.is_file(), "尚未下载真实东坡模型")
class DongpoRealModelTests(unittest.TestCase):
    def test_pinned_model_loads_and_scores(self):
        judge = dongpo._validate_model(dongpo.MODEL_PATH.read_bytes())
        self.assertGreater(len(judge.vocabulary), 1000)
        self.assertTrue(0 < judge.threshold < 1)
        # 固定权重由原版 TypeScript 推理得到的参考分数，同时覆盖门控内的正常表达。
        samples = (
            ("服务已经恢复了", 0.45287869054514235, False),
            ("已经修好了，但这不代表以后不会出错。", 0.5980222301879938, True),
            ("仅凭这些还不能下结论。", 0.5199199648542604, True),
            ("不需要重启，修改配置后会自动生效。", 0.3853644930793949, False),
            ("这不是网络问题，而是配置错误。", 0.4435976727434063, False),
        )
        for text, expected_score, rejected in samples:
            with self.subTest(text=text):
                self.assertAlmostEqual(judge.score(text), expected_score, 14)
                self.assertEqual(dongpo.detect(text), rejected)


if __name__ == "__main__":
    unittest.main()
