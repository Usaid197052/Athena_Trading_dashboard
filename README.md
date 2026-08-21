# Athena Trading Dashboard

Windows desktop desk for **MetaTrader 5 demo** analysis and (optional) auto-trade. A local multi-agent pipeline can review a symbol and write a decision-support note on the HUD. **Agent output never places an order.**

Text only: type commands or use the HUD buttons. There is no microphone on this path.

## What you get

| Piece | Role |
| --- | --- |
| **Athena** (Gemini Flash) | Orchestrator: starts the run, writes the short narrative |
| **Qwen 2.5 Coder 7B** (Ollama) | Technical interpretation of existing NumPy indicators |
| **DeepSeek-R1 7B** (Ollama) | News / fundamental read |
| **Desk watch loop** | Existing demo auto-trade scorecard (`watch_tick`) — separate from Analyze |

Graph agent is a disabled stub. Do not enable it unless you have pulled that model yourself.

---

## Requirements

- Windows 10/11
- Python 3.12
- [MetaTrader 5](https://www.metatrader5.com/) logged into a **demo** account (live `order_send` is refused)
- [Ollama](https://ollama.com/) running locally (`http://127.0.0.1:11434`)
- A [Google Gemini API key](https://aistudio.google.com/apikey)
- NVIDIA GPU recommended for the 7B models (8 GB VRAM is enough if only one model is loaded at a time)

---

## Install

From the repo root:

```powershell
python -m pip install -r requirements.txt
```

---

## Ollama models

Point Ollama at the models folder and force the NVIDIA CUDA backend (needed on Intel+NVIDIA hybrid laptops so Vulkan does not park models in system RAM):

```powershell
[System.Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "E:\Ollama models", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_LLM_LIBRARY", "cuda_v13", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_VULKAN", "0", "User")
$env:OLLAMA_MODELS = "E:\Ollama models"
$env:OLLAMA_LLM_LIBRARY = "cuda_v13"
$env:OLLAMA_VULKAN = "0"
```

Restart Ollama (or reboot) so the service picks up those variables. Then pull the two required models:

```powershell
ollama pull deepseek-r1:7b
ollama pull qwen2.5-coder:7b
ollama list
```

Confirm Ollama is up before starting Athena:

```powershell
ollama list
```

Path and model names also live in `config/agents.json`. Leave `graph_agent.enabled` as `false` unless you intentionally add a graph model.

---

## Gemini API key

1. Start the desk (see below). On first launch the settings overlay asks for a key.
2. Paste a Gemini key, give it a label, click **ADD**, then **ACTIVATE**.
3. **TEST** the key, pick a **Flash** model, save.

Keys are stored encrypted with Windows DPAPI in gitignored `config/api_keys.json`. You can add several keys and switch the active one from Settings.

---

## MetaTrader 5

1. Open MT5 and log into a **demo** account.
2. Make sure the symbols you care about are in Market Watch (right-click → Show All if a quote is missing).
3. Leave MT5 running while Athena is open. The desk attaches over MT5’s local IPC.

Default watch list is `EURUSD` on `H1` (`config/trading.json`).

---

## Start the desk

```powershell
cd D:\AI\Athena
python trading_main.py
```

Or double-click `autostart.bat`.

The HUD should show **LISTENING**. Agent status dots sit under the header (Athena / DeepSeek / Qwen). Graph stays disabled.

---

## Using the HUD

**Buttons**

| Button | Effect |
| --- | --- |
| **ANALYZE** | Runs the multi-agent pipeline on the configured symbol/timeframe. Decision support only. |
| **PAUSE** | Stops the demo auto-trade watch loop |
| **RESUME** | Starts auto-trade again |
| **FLATTEN** | Closes Athena demo positions (magic `20260820`) |

After Analyze, the content panel gets an **ANALYSIS** block (overall bias, confidence, disagreement, short why/note). That block is separate from the desk **LAST** line used by auto-trade.

**Type in the input box** (Enter to send). Ordinary questions go to **Athena (Gemini Flash)** using the current desk snapshot. Commands below still take priority:

| Command | What it does |
| --- | --- |
| `analyze` | Same as the Analyze button (uses `config/trading.json` symbol + timeframe) |
| `analyze EURUSD H1` | Analyze a specific symbol and timeframe (`M1` `M5` `M15` `M30` `H1` `H4` `D1` `W1`) |
| `analyse` / `review` / `assess` | Same as `analyze` |
| `quote` / `quote GBPUSD` | Live MT5 quote |
| `status` | Desk status |
| `run desk` | One-shot **existing** scorecard (this *can* trade on demo if auto-trade rules allow) |
| `pause` / `resume` / `flatten` | Same as the buttons |
| `sleep` | Hide to tray and pause auto-trade. Wake from the tray icon. |
| `help` or `?` | Print the command list |
| `shutdown` / `quit athena` | Exit the process |

Anything else is a chat with Athena. Ask about the last Analyze — for example “how confident are you on this trade?” She quotes specialist confidence with **technical analysis leading**. Chat itself never places an order.

Sleep does not shut the process down; use the tray icon to wake.

---

## What Analyze does (and does not)

Order of work, one 7B model at a time:

1. Pull MT5 bars + news (CPU)
2. Compute indicators in Python (EMA, RSI, MACD, ATR, Bollinger, swings)
3. Load Qwen → technical interpretation → unload
4. Load DeepSeek → fundamental / news → unload
5. Python conflict rules: **technical leads**; neutral/weak news follows the chart; only high-impact fresh news can veto
6. Gemini Flash writes a short narrative onto the HUD

The **Analyze** button alone does not call `order_send`. When `"auto_trade": true`, the watch loop (or `run desk`) runs the same agent rules and may place a **demo** order after gates (session, news blackout, risk, ATR stops).

Default symbol is **EURUSD**. Use `analyze GBPUSD H1` (or another pair) for other markets.

If you want analysis only, set `"auto_trade": false` in `config/trading.json` and restart.

---

## Config you may actually change

**`config/trading.json`** — desk / auto-trade

- `symbols`, `timeframe` — default Analyze + watch loop
- `auto_trade` — `true` to let the watch loop place demo trades
- `volume`, `sl_atr`, `tp_atr`, `max_positions`, `daily_loss_pct`, `max_spread_points`
- `watch_interval_sec` — how often the watch loop runs (minimum 8s)

**`config/agents.json`** — local models

- `models_dir` — should match `OLLAMA_MODELS`
- `fundamental.model` / `technical.model`
- Timeouts and `keep_alive` (`0s` unloads after each call so the 8 GB card can swap models)

Restart Athena after editing either file.

---

## Logs

Under `logs/trading/`:

| File | Audience |
| --- | --- |
| `activity.log` | Plain-English “what just happened” |
| `agents.debug.log` / `agents.jsonl` | Timings, model tags, validation (secrets redacted) |
| `desk.log` / `decisions.jsonl` | Auto-trade scorecard |
| `analysis.log` / `analysis.jsonl` | Desk analysis snapshots |

API keys are stripped from these streams.

---

## Tests

```powershell
python -m pytest tests -q
```

A live Ollama ping test is marked `slow` and is skipped in the default run.

---

## Typical first-run checklist

1. MT5 demo is open and quoting `EURUSD`.
2. Ollama is running; `ollama list` shows `deepseek-r1:7b` and `qwen2.5-coder:7b`.
3. `python trading_main.py`
4. Add / activate / test a Gemini Flash key in Settings.
5. Click **ANALYZE** (or type `analyze EURUSD H1`).
6. Read the **ANALYSIS** block. Treat it as a note, not an order.

A full run can take a minute or two on GPU, longer on CPU.

---

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `Save a Gemini API key in Settings` | Add + Activate a key; Test it |
| Ollama errors / agent skipped | `ollama list`; Ollama app running; `OLLAMA_MODELS` still `E:\Ollama models` after reboot |
| Very slow Analyze / `ollama ps` shows `100% CPU` | Set `OLLAMA_LLM_LIBRARY=cuda_v13` and `OLLAMA_VULKAN=0` (User env), keep `OLLAMA_MODELS=E:\Ollama models`, fully quit and restart Ollama. Confirm with `ollama ps` → `100% GPU` and `nvidia-smi` VRAM while a model is loaded. |
| Empty quote / no bars | Symbol visible in MT5 Market Watch; demo terminal logged in |
| Analyze finished but no trade | Agents said WAIT, gates blocked, or auto_trade is off. Check confidence / disagreement. |
| `analysis already running` | Wait; only one Analyze at a time |
| Sleep will not wake | Right-click the tray icon → wake (not “Hey Athena” — there is no mic) |

---

## Design notes

Architecture write-up: `docs/Athena_Trading_Multi_Agent_Architecture.docx`  
Regenerate it with `python docs/generate_architecture_docx.py`.
