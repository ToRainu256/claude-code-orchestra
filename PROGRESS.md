# PROGRESS

> Auto-maintained by /checkpointing. Shows the most recent 5 checkpoints (newest first).
> Full checkpoints live in `.claude/checkpoints/` (git-ignored).

## [2026-11-19-055210](.claude/checkpoints/2026-11-19-055210.md)

### 何をしたのか
- このリポジトリ自身のプロジェクト状態を初めて実体化した（ドッグフーディング開始、設計書の PR 7）。`/init` で `.claude/docs/DESIGN.md`（背景・スコープ・機能要件 6 件・非機能要件 4 件・技術選定 5 件・エージェントロール 6 件・制約・TODO）と `.claude/STATE.md` の Repository Identity を書き、`design-tracker` でフォーク運用の設計判断 8 件と「Development Procedure (this repository)」を記録した。
- `scripts/install.sh` の `GITIGNORE_ENTRIES` に `.claude/worktrees/` を追加し、導入先プロジェクトでもエージェント worktree が無視されるようにした（テスト `test_install_ignores_agent_worktrees` を追加、`TEMPLATE_DESIGN_LOG.md` に 1 行記録）。
- これまでのフェーズ: ゲートのポータブル化と CI（#1）、モデルマニフェスト（#2）、ロール改名と lint（#4）、Codex 不在時の代替ルール（#5）、エージェント worktree ディレクトリの許可（#6）、scaffold 分離（#7）、メンテナンスジョブとモデル監視（#9、auto-merge 待ちで open）。

### どういうやり取りをユーザーと行ったのか
- オーナー承認済みの設計（automation-and-dogfooding-design の B1・B2・PR 7）と、記録すべき 8 件の設計判断・チェックポイント内容がオーケストレーター経由で指示された。リポジトリの識別情報は指示に含まれており、追加の質問はしていない。
- 状態ファイルが導入先に漏れないことを書き込み前に確認し報告することが求められた。

### どうやったのか
- `detect_stack.py` で証拠を集め、`update_design.py` の dry-run プレビューを確認してから `--apply --require-change` で書き込んだ。Identity は `append_state_block.py --type repository-identity` で書いた。
- `scripts/install.sh` / `scripts/update.sh` のコピー対象を読み、STATE・DESIGN は `scripts/scaffold/` の seed からのみ作られ、`PROGRESS.md` と `.claude/checkpoints/*` はどちらのリストにも無いことを確認した。モデル名は manifest 以外に書かず、ロール名で記述した。

### 途中でどういう課題が起こったのか
- `update_design.py` の `section_updates` は追記のみで、テンプレートの空の箇条書き `- ` が In Scope・Out of Scope・制約・TODO に残る。
- アーキテクチャ節への追記は Agent Roles 表の後ろに入るため、`### Layout` 小見出しを付けて回避した。
- 識別情報の「Python 3.9 互換」は、`pyproject.toml` の `>=3.11` と `_shared` スクリプトの `datetime.UTC` 利用と食い違うため、DESIGN.md の TODO に未解決事項として残した。

### 将来のアクション
- ランチャーのメッセージ修正（issue #10、R2）を行い、launchd ジョブをオーナー立ち会いでインストールする。
- 配布ステップ（distribution）を実装する。
- upstream 取り込みヘルパーとルーティン、issue-worker ルーティンを作る。
- issue #3、#8、#10 に対応する。
