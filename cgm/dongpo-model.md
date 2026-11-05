# 东坡语法检测模型来源

- 上游：[chiyuki0325/Edelweiss](https://github.com/chiyuki0325/Edelweiss)
- 固定提交：[1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06](https://github.com/chiyuki0325/Edelweiss/commit/1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06)
- [原始权重](https://raw.githubusercontent.com/chiyuki0325/Edelweiss/1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06/src/driver/dongpo/model.json)：上游标注为 `dongpo-judge/artifacts/tuned-v1/model.json` 的未修改导出文件。
- 格式：`dongpo-judge-char-tfidf-lr`，版本 1。
- 字节数：`1437772`。
- Git blob SHA-1：`aaf046b77ed499a3189a2c54b12e4f80fc157050`。

Python 推理参考上游 [`src/driver/dongpo/index.ts`](https://github.com/chiyuki0325/Edelweiss/blob/1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06/src/driver/dongpo/index.ts)，
保留字符 n-gram、大小写和空白归一化、sublinear TF、IDF、L2 归一化、逻辑回归概率及严格阈值比较。
权重不作转换、训练或修改；由 `python -m cgm.dongpo --download` 下载并校验，随 Docker 镜像部署。

Git blob 校验计算为 `SHA1(b"blob " + str(len(data)).encode() + b"\0" + data)`，并非文件本身的 SHA-1。
