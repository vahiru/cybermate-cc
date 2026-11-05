"""本地字符 TF-IDF / logistic regression，检测 GPT 式防御性结尾。

模型固定为 Edelweiss 1b5fafab 中的 dongpo-judge tuned-v1，推理只用标准库。
本地开发先运行 python -m cgm.dongpo --download；镜像构建时自动下载。
"""

import argparse
import hashlib
import json
import math
import re
import threading
import urllib.request
from collections import Counter
from functools import cache
from pathlib import Path

MODEL_PATH = Path(__file__).with_name("dongpo-model.json")
MODEL_COMMIT = "1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06"
MODEL_BLOB = "aaf046b77ed499a3189a2c54b12e4f80fc157050"
MODEL_SIZE = 1437772
MODEL_URL = (f"https://raw.githubusercontent.com/chiyuki0325/Edelweiss/{MODEL_COMMIT}"
             "/src/driver/dongpo/model.json")
MODEL_SETUP = ("缺少东坡模型；先运行 python -m cgm.dongpo --download，"
               "或用 CGM_NOT_DONGPO=0 关闭检测")
GATE = re.compile(r"[不但仅凭只尚却]|无法|更准确")
_load_lock = threading.Lock()


class DongpoJudge:
    def __init__(self, model: dict):
        if (model.get("format") != "dongpo-judge-char-tfidf-lr"
                or model.get("format_version") != 1):
            raise ValueError("不支持的东坡模型格式")
        if (len(model["features"]) != len(model["idf"])
                or len(model["features"]) != len(model["coefficients"])):
            raise ValueError("东坡模型特征维度不一致")
        self.vocabulary = {feature: i for i, feature in enumerate(model["features"])}
        self.idf = model["idf"]
        self.coefficients = model["coefficients"]
        self.intercept = model["intercept"]
        self.threshold = model["threshold"]
        self.min_ngram, self.max_ngram = model["ngram_range"]
        self.lowercase = model["lowercase"]
        self.sublinear_tf = model["sublinear_tf"]

    def score(self, text: str) -> float:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("输入必须包含非空文本")
        normalized = re.sub(r"\s{2,}", " ", text.lower() if self.lowercase else text)
        counts: Counter[int] = Counter()
        for size in range(self.min_ngram, min(self.max_ngram, len(normalized)) + 1):
            for offset in range(len(normalized) - size + 1):
                index = self.vocabulary.get(normalized[offset:offset + size])
                if index is not None:
                    counts[index] += 1
        values = [(i, (1 + math.log(count) if self.sublinear_tf else count) * self.idf[i])
                  for i, count in sorted(counts.items())]
        norm = math.sqrt(sum(value * value for _, value in values))
        decision = self.intercept
        if norm:
            for index, value in values:
                decision += value / norm * self.coefficients[index]
        if decision >= 0:
            return 1 / (1 + math.exp(-decision))
        exponential = math.exp(decision)
        return exponential / (1 + exponential)


def _validate_model(data: bytes) -> DongpoJudge:
    blob = b"blob " + str(len(data)).encode() + b"\0" + data
    if len(data) != MODEL_SIZE or hashlib.sha1(blob).hexdigest() != MODEL_BLOB:
        raise ValueError("东坡模型完整性校验失败，请重新下载固定版本")
    return DongpoJudge(json.loads(data))


@cache
def _load_judge() -> DongpoJudge:
    try:
        data = MODEL_PATH.read_bytes()
    except FileNotFoundError as e:
        raise RuntimeError(MODEL_SETUP) from e
    return _validate_model(data)


def load_judge() -> DongpoJudge:
    # cache 本身可能并发执行首次调用，用锁确保进程内只加载一份。
    with _load_lock:
        return _load_judge()


def detect(text: str) -> bool:
    if not GATE.search(text):
        return False
    judge = load_judge()
    tail = text[-120:]  # Python 字符串切片以 Unicode code point 为单位。
    return bool(tail.strip()) and judge.score(tail) > judge.threshold


def download_model() -> None:
    if MODEL_PATH.is_file():
        try:
            _validate_model(MODEL_PATH.read_bytes())
            return
        except ValueError:
            pass  # 损坏的旧文件在新下载通过校验后才替换。
    with urllib.request.urlopen(MODEL_URL, timeout=120) as response:
        data = response.read(MODEL_SIZE + 1)
    _validate_model(data)
    temporary = MODEL_PATH.with_suffix(".json.tmp")
    temporary.write_bytes(data)
    temporary.replace(MODEL_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", required=True)
    parser.parse_args()
    download_model()
    print(f"东坡模型已下载并校验：{MODEL_PATH}")
