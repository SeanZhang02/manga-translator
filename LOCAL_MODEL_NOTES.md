# 本地模型注记 — manga-translator

> [2026-06-06] local-model retro (Sean AFK). 仅文档, 任何代码/模型改动 defer 给 Sean. 权威速查见 Claude memory `reference_local_model_stack_5090.md`; 研究原文 `C:/Claude Code Beta/local-llm-5090-research-2026-06-06.md`.

## 现状
- Backend 默认 `ollama`, model `gemma4:26b-a4b-it-q4_K_M` (本地 **Gemma 4 26B-A4B MoE vision** via Ollama, `batch.py` default_config).
- payload 设 `think:False` (`batch.py:744`, 防 Gemma thinking 吃掉 6000-token 预算返回空), `num_predict:6000`, temperature 0.2. 输出 = JSON-array + fence-strip / slice / json_repair / 1-retry 硬化 — 与 Second Brain daemon 同款防护。
- 可选云 backend: claude-opus-4-7 / claude-sonnet-4-6.

## 机器状态 (2026-06-06 实测)
- RTX 5090 32GB; Ollama **0.20.6 OUTDATED** (最新 0.30.6, 新版 better vision); 已装唯一模型即此 tag (零 pull 可跑); C 盘 ~504GB free.
- 磁盘无 LM Studio / llama.cpp / ComfyUI.

## 待 Sean (defer — install/model-pull/行为改动, 明日更大范围一起做)
1. 升 Ollama 0.20.6 → 0.30.x: vision 调用提速 + 提质。
2. **别 naive 换 Qwen3.6 做 vision** — Qwen3.6 的 Ollama vision 仍 rough。要换得迁 llama.cpp/LM Studio 并在 bbox 任务上重验 PROMPT_TMPL。短期保持 Gemma 4 via Ollama。
