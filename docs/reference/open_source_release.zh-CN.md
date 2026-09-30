# 开源依赖与发布政策

[English](open_source_release.md)

本文规定 BFBT 如何管理依赖、版本、tag 和可下载软件包；文档本身不构成发布授权。

## 受支持的源码与安装状态

- `main` 是当前开发与安全修复主线；
- Git tag `v主版本.次版本.修订版本` 对应一次正式发布的源码状态；
- GitHub Release 里的产物是受支持的不可变二进制包与源码包；
- `requirements/runtime.lock` 是最终用户使用的 hash 锁定环境；
- `requirements/dev.lock` 额外包含 CI 使用的测试与发布工具；
- 不使用对应锁文件的 editable 安装只是方便开发，不属于可复现发布环境。

锁文件覆盖 Python 3.10–3.12 和 Linux 兼容产物，同时固定直接/间接依赖版本与 hash。
`requirements/lock-manifest.json` 记录输入 hash、锁文件 hash 和精确的 `uv` 生成器版本；
`scripts/release_tools.py check-lock` 可以完全离线验证这些事实。

## 可复现安装

运行环境先按锁文件安装依赖，再关闭依赖解析安装 BFBT：

```bash
python -m pip install --require-hashes -r requirements/runtime.lock
python -m pip install --no-deps --no-build-isolation -e .
```

CI 和 Release 对 `requirements/dev.lock` 采用同一方法。缺少 hash、输入变化、手工修改锁文件、
Python 版本不兼容或锁定产物不可用时必须失败关闭。Ubuntu 安装脚本使用 runtime 路线，不会
在失败后静默退回浮动依赖安装。

## 依赖更新

Dependabot 每周检查 Python 和 GitHub Actions 依赖，但项目不启用自动合并。每次更新必须：

1. 解释直接依赖和重要间接依赖的变化；
2. 使用 manifest 指定的精确生成器重新生成两份锁文件；
3. 检查软件包来源、许可证、Python 支持范围和意外新增依赖；
4. 通过锁文件门和完整 Python 3.10/3.12 离线测试；
5. 除非有显式版本化合同变更，否则保持经济等价和 artifact 身份。

安全修复可以立即处理。Polars、PyArrow、DuckDB、Pydantic、打包工具、GitHub 发布 Action
和其他 major 更新必须单独建立 PR。不能仅因为机器人认为“版本较新”就接受更新。

锁文件重建属于明确联网的维护动作，步骤见 `requirements/README.md`。依赖更新绝不能夹带
生成行情、回测产物或其他本地数据。

## 版本与兼容性

BFBT 的 Python distribution 和 Git tag 使用语义化版本。项目仍处于 `0.x` 时，次版本可以
调整未记录为公共合同的 Python 内部接口；修订版本不应故意改变已经公开的研究经济语义。

Schema、因子、数据集、研究和 run 分别拥有自己的显式版本。软件包发布不能重写不可变产物，
也不能静默改变其已经记录的经济含义。公开 CLI 或合同发生破坏性变化时，必须写入发布说明；
条件允许时至少提前一个次版本给出弃用提示。

`pyproject.toml`、`bfbt.__version__`、tag 和 Changelog 标题中的版本必须一致；
`scripts/release_tools.py check-tag` 会强制校验。

## 发布流程

1. 从同步、干净的 `main` 创建功能分支；
2. 在 `pyproject.toml` 和 `src/bfbt/__init__.py` 写入相同版本；
3. 把两个 Changelog 的用户可见内容从 `Unreleased` 移入 `## X.Y.Z - YYYY-MM-DD`；
4. 如果依赖输入发生变化，重新生成锁文件；
5. 通过聚焦检查、完整 Python 3.10/3.12 离线测试、构建、`twine check`、wheel/sdist 内容
   检查、秘密/路径扫描和文档检查；
6. 合并 `main` 后，在该精确 commit 上创建并推送 annotated 或 signed `vX.Y.Z` tag；
7. `release` 工作流重新校验 tag 和锁文件、再次运行两套测试、使用锁定工具链构建、生成
   `SHA256SUMS`，并把 wheel、sdist 和校验文件发布到同一个 GitHub Release。

工作流不会自行创建 tag；创建 tag 是维护者明确作出的发布决定。失败任务不会发布半成品；
应通过新的 commit 和版本决定修复，不能移动已经公开的 tag。

当前未启用 PyPI 发布。只有配置 PyPI Trusted Publishing 和受保护 GitHub environment 后才
能增加，禁止使用长期上传 token。在这套基础设施另行完成和验证前，GitHub Release 产物是
权威安装包。

## 仓库所有者需要配置的设置

GitHub 仓库设置无法通过源码提交。所有者应保护 `main`、要求离线测试通过后才能合并、禁止
force push 和删除，并把 release tag 创建权限限制给维护者。当前只发布 GitHub Release，
工作流需要创建 Release 的权限，但不需要额外仓库 secret。

## 回滚与安全

已经公开的 tag 和产物保持不可变。如果发布存在缺陷，应在 GitHub 中显著标注，再发布修复
后的 patch 版本，并保留原始证据。安全问题遵循 `SECURITY.md`；在协调修复前，不要在
Changelog 公开利用细节。
