# Sibyl — Study Guide

A deep-dive into how every piece of Sibyl works, from audio capture to AI processing. Read this to understand the full flow well enough to rebuild it from scratch.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Entry Point: CLI](#2-entry-point-cli)
3. [Audio Recording: recorder.py](#3-audio-recording-recorderpy)
4. [Live Transcription: live.py](#4-live-transcription-livepy)
5. [The LangGraph Pipeline: graph.py](#5-the-langgraph-pipeline-graphpy)
6. [Pipeline State: state.py](#6-pipeline-state-statepy)
7. [Node: Transcribe](#7-node-transcribe)
8. [Node: Summarize](#8-node-summarize)
9. [Node: Extract Actions](#9-node-extract-actions)
10. [Node: Detect Emotions](#10-node-detect-emotions)
11. [Shared Client: nodes/__init__.py](#11-shared-client-nodes__init__py)
12. [Configuration: config.py](#12-configuration-configpy)
13. [Logging: log.py](#13-logging-logpy)
14. [Data Flow Diagrams](#14-data-flow-diagrams)
15. [Key Concepts](#15-key-concepts)
16. [How to Extend](#16-how-to-extend)

---

## 1. System Overview

Sibyl has two independent modes that share some components:

```
┌─────────────────────────────────────────────────────────────────┐
│                            SIBYL                                │
│                                                                 │
│  ┌──────────────┐     ┌──────────────────────────────────────┐  │
│  │  LIVE MODE   │     │           BATCH MODE                 │  │
│  │              │     │                                      │  │
│  │  live.py     │     │  recorder.py → graph.py → nodes/*   │  │
│  │  (WebSocket) │     │  (record)      (LangGraph pipeline)  │  │
│  │              │     │                                      │  │
│  │  Gemini Live │     │  Gemini 2.5 Flash (REST API)        │  │
│  │  API         │     │                                      │  │
│  └──────┬───────┘     └──────────────┬───────────────────────┘  │
│         │                            │                          │
│         └────────────┬───────────────┘                          │
│                      │                                          │
│              ┌───────▼────────┐                                 │
│              │    cli.py      │                                 │
│              │  (argparse)    │                                 │
│              └───────┬────────┘                                 │
│                      │                                          │
│              ┌───────▼────────┐                                 │
│              │   config.py    │                                 │
│              │   log.py       │                                 │
│              └────────────────┘                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Live mode** bypasses LangGraph entirely — it's a direct WebSocket connection to Gemini's Live API. The `-s` flag optionally chains into the batch pipeline after recording stops.

**Batch mode** uses LangGraph to orchestrate four Gemini API calls (transcribe, summarize, extract, emotions) with the last three running in parallel.

---

## 2. Entry Point: CLI

**File:** `src/sibyl/cli.py`

The CLI uses Python's `argparse` with subcommands. Each subcommand maps to a handler function:

```
main()
  ├── parser.add_subparsers()
  │     ├── "record"  → cmd_record(args)
  │     ├── "process" → cmd_process(args)
  │     ├── "live"    → cmd_live(args)
  │     └── "list"    → cmd_list(args)
  └── args.func(args)   ← dispatches to the right handler
```

### pyproject.toml wiring

```toml
[project.scripts]
sibyl = "sibyl.cli:main"
```

When you run `uv run sibyl`, uv creates a script in `.venv/bin/sibyl` that calls `cli.main()`. This is a standard Python entry point — nothing uv-specific.

### Handler flow

**`cmd_record(args)`:**
1. `load_config()` → creates a `Config` dataclass from env vars
2. `record_meeting(config)` → blocks until Ctrl+C, returns `Path` to WAV
3. If `--process` flag: `asyncio.run(process_meeting(audio_path))` → runs LangGraph pipeline
4. `_print_results(result)` → prints and saves JSON

**`cmd_live(args)`:**
1. `load_config()` → Config
2. `asyncio.run(live_transcribe(config))` → WebSocket streaming, returns dict with transcript
3. If `--summarize` flag: `asyncio.run(process_meeting(result["audio_path"]))` → LangGraph pipeline
4. `_print_results(result)`

**`cmd_process(args)`:**
1. Validates file exists
2. `asyncio.run(process_meeting(audio_path))` → LangGraph pipeline directly

**`_print_results(result)`:**
Pretty-prints every field from the pipeline state and writes a `.json` file next to the `.wav`.

---

## 3. Audio Recording: recorder.py

**File:** `src/sibyl/recorder.py`

This is the most hardware-dependent part. It captures audio from two sources simultaneously.

### Device discovery

```python
def _get_default_monitor() -> str | None:
    # Runs: pactl get-default-sink
    # Returns: "{sink_name}.monitor"
    # Example: "alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink.monitor"

def _get_default_source() -> str | None:
    # Runs: pactl get-default-source
    # Returns the current default mic
    # Example: "alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source"
```

These use `subprocess.run(["pactl", ...])` to query PipeWire/PulseAudio. The key insight: **system audio is captured via a "monitor" source** — PipeWire mirrors whatever goes to your speakers/headphones into a virtual input source. This means:

- Speaker active → `Speaker__sink.monitor` captures meeting audio
- Headphones plugged in → `Headphone__sink.monitor` captures meeting audio
- The recorder follows dynamically because it queries the *current* default sink

### `_get_device_index(device_name)`

Sounddevice uses integer indices, not PulseAudio names. This function iterates `sd.query_devices()` and matches by substring.

### Dual-source recording

```python
def record_meeting(config: Config) -> Path:
```

**The threading model:**

```
Main Thread                 Thread 1 (daemon)          Thread 2 (daemon)
    │                           │                          │
    ├── resolve devices         │                          │
    ├── create stop_event       │                          │
    ├── start Thread 1 ────────►│ _record_stream()         │
    ├── start Thread 2 ────────►│   sd.InputStream(        │ _record_stream()
    │                           │     device=monitor)      │   sd.InputStream(
    │   [blocks on t.join()]    │   while not stop:        │     device=mic)
    │                           │     stream.read(1s)      │   while not stop:
    │   ← Ctrl+C ──────────────│     frames.append()      │     stream.read(1s)
    │   stop_event.set()        │   ← exits loop           │     frames.append()
    │   join threads            │                          │   ← exits loop
    │                           │                          │
    ├── mix audio               │                          │
    │   monitor[:min_len]       │                          │
    │   + mic[:min_len]         │                          │
    │   normalize to 0.95 peak  │                          │
    ├── sf.write(WAV)           │                          │
    └── return Path             │                          │
```

**Why two threads instead of two async tasks?** `sounddevice.InputStream` is a C library wrapper that blocks the calling thread. It can't be `await`ed. `asyncio.to_thread` would work but the signal handling for Ctrl+C is simpler with raw threads.

**Audio mixing:**
```python
audio = monitor_audio[:min_len] + mic_audio[:min_len]  # element-wise addition
peak = np.abs(audio).max()
audio = audio / peak * 0.95  # normalize to prevent clipping
```

Both streams are float32 numpy arrays with values in [-1.0, 1.0]. Adding them can exceed that range, so we normalize. The `0.95` ceiling prevents digital clipping.

### Signal handling

```python
signal.signal(signal.SIGINT, _sigint_handler)  # Install custom handler
# ... recording loop ...
signal.signal(signal.SIGINT, original_handler)  # Restore original
```

We intercept `SIGINT` (Ctrl+C) to set the stop event gracefully instead of crashing. The original handler is restored after recording so the rest of the program handles Ctrl+C normally.

---

## 4. Live Transcription: live.py

**File:** `src/sibyl/live.py`

This is completely separate from the LangGraph pipeline. It uses the **Gemini Live API** — a bidirectional WebSocket protocol.

### Connection setup

```python
client = genai.Client(api_key=config.gemini_api_key)

live_config = types.LiveConnectConfig(
    response_modalities=["AUDIO"],          # MUST be AUDIO — TEXT causes 1011 error
    input_audio_transcription=...,          # Enables text transcription of what we send
    output_audio_transcription=...,         # Enables text transcription of model's response
    system_instruction="...",               # "Be a silent transcriber, learn speaker names"
)

async with client.aio.live.connect(
    model="gemini-3.1-flash-live-preview",
    config=live_config,
) as session:
    ...
```

**Critical detail:** `response_modalities=["AUDIO"]` is mandatory. Setting it to `["TEXT"]` causes a 1011 WebSocket error. The Live API always responds with audio — we get text via the `input_audio_transcription` and `output_audio_transcription` config options, which give us text alongside the audio response.

### The streaming loop

Two async tasks run concurrently:

```
┌─────────────────────────────────────────────────────────┐
│                  asyncio event loop                      │
│                                                         │
│  ┌─────────────────┐          ┌──────────────────────┐  │
│  │   send_audio()  │          │ receive_transcript() │  │
│  │                 │          │                       │  │
│  │ audio_queue ────┼──WSS──→  │  ←──WSS──── session  │  │
│  │   .get()        │  send    │   .receive()         │  │
│  │                 │          │   print(text)        │  │
│  └────────▲────────┘          └──────────────────────┘  │
│           │                                             │
│  ┌────────┴────────┐                                    │
│  │ _audio_callback │  ← sounddevice calls this from    │
│  │  (C thread)     │    its own audio thread            │
│  │  float32→PCM16  │                                    │
│  │  queue.put()    │                                    │
│  └─────────────────┘                                    │
└─────────────────────────────────────────────────────────┘
```

**Audio format conversion:**
```python
pcm16 = (indata * 32767).astype("<i2")  # float32 [-1,1] → int16 little-endian
```

The `"<i2"` dtype means: little-endian (`<`), signed integer (`i`), 2 bytes (`2`). This is what the Gemini Live API requires: raw 16-bit PCM, 16kHz, mono, little-endian.

**Extracting transcription from responses:**
```python
async for response in session.receive():
    sc = response.server_content
    if sc.input_transcription and sc.input_transcription.text:
        print(text, end="", flush=True)  # Real-time output
        transcript_parts.append(text)
```

`input_transcription` is what the model heard (our audio). `output_transcription` is what the model said back (we tell it to stay silent, so this is mostly empty).

---

## 5. The LangGraph Pipeline: graph.py

**File:** `src/sibyl/graph.py`

### What is LangGraph?

LangGraph is a framework for building stateful, graph-based workflows. Think of it as:
- **Nodes** = async functions that transform state
- **Edges** = connections that define execution order
- **State** = a TypedDict that flows through the graph
- **Compile** = turns the graph definition into an executable

### Graph definition

```python
def build_graph() -> StateGraph:
    graph = StateGraph(MeetingState)

    # Register nodes (name → async function)
    graph.add_node("transcribe", transcribe)
    graph.add_node("summarize", summarize)
    graph.add_node("extract_actions", extract_actions)
    graph.add_node("detect_emotions", detect_emotions)

    # Define edges
    graph.set_entry_point("transcribe")
    graph.add_edge("transcribe", "summarize")         # transcribe → summarize
    graph.add_edge("transcribe", "extract_actions")   # transcribe → extract (parallel)
    graph.add_edge("transcribe", "detect_emotions")   # transcribe → emotions (parallel)

    # Fan-in: all three must complete before END
    graph.add_edge(["summarize", "extract_actions", "detect_emotions"], "__end__")

    return graph
```

### Execution flow

```
Step 1:  __start__ → transcribe
            │
            │  Gemini API call: audio → text
            │  Updates state: { transcript: "..." }
            │
Step 2:  transcribe → [summarize, extract_actions, detect_emotions]
            │              │                          │
            │  (parallel)  │      (parallel)          │  (parallel)
            │  Gemini API  │      Gemini API          │  Gemini API
            │  summary     │      action_items        │  speaker_emotions
            │              │      decisions            │  meeting_mood
            │              │      participants         │
Step 3:  [all three complete] → __end__
```

**How parallelism works in LangGraph:**
When multiple edges leave the same node, LangGraph runs the target nodes concurrently using `asyncio.gather()` under the hood. The fan-in edge (`["a", "b", "c"] → __end__`) waits for all three to complete before proceeding. Each node receives the **same** state snapshot from after `transcribe` completed.

### State merging

Each parallel node returns a dict of state updates. LangGraph merges them:
```python
# summarize returns:  {"summary": "..."}
# extract returns:    {"action_items": [...], "decisions": [...], "participants": [...]}
# emotions returns:   {"speaker_emotions": [...], "meeting_mood": "..."}
# 
# Final state = original state + all three merged (no key conflicts)
```

### `process_meeting()` and `compile_graph()`

```python
def compile_graph():
    return build_graph().compile()  # Returns a CompiledStateGraph

async def process_meeting(audio_path):
    app = compile_graph()
    result = await app.ainvoke({"audio_path": str(audio_path)})
    return result
```

`ainvoke()` runs the full graph asynchronously and returns the final state dict. This is the only function the CLI calls — it doesn't know about individual nodes.

---

## 6. Pipeline State: state.py

**File:** `src/sibyl/state.py`

```python
class MeetingState(TypedDict, total=False):
    audio_path: str              # Input: path to WAV file
    transcript: str              # After transcribe node
    summary: str                 # After summarize node
    action_items: list[str]      # After extract node
    decisions: list[str]         # After extract node
    participants: list[str]      # After extract node
    speaker_emotions: list[dict] # After emotion node
    meeting_mood: str            # After emotion node
```

**Why `total=False`?** This makes all fields optional. At the start, only `audio_path` exists. Each node adds its fields. Without `total=False`, LangGraph would expect all fields to be present when passing state between nodes, which would fail.

**This TypedDict is the contract** between all nodes. Every node receives the full state and returns a dict with only the keys it wants to update. LangGraph handles the merge.

---

## 7. Node: Transcribe

**File:** `src/sibyl/nodes/transcribe.py`

```python
async def transcribe(state: MeetingState) -> dict:
```

1. Reads the WAV file as bytes
2. Base64-encodes it (Gemini's REST API requires inline data as base64)
3. Sends to Gemini with a prompt asking for verbatim transcription with speaker names
4. Returns `{"transcript": response.text}`

**Why base64?** The Gemini REST API uses JSON, which can't contain raw binary. Base64 encoding converts binary to ASCII text at a ~33% size overhead. For a 10-minute meeting at 16kHz mono, the WAV is ~19MB, base64 is ~25MB.

**Speaker identification prompt:**
```
"When speakers introduce themselves, use their actual names as labels
(e.g., 'Joseph:', 'Mark:') instead of generic labels like 'Speaker 1'."
```

This works because Gemini processes the full audio holistically — it can associate voice characteristics with names mentioned in introductions.

---

## 8. Node: Summarize

**File:** `src/sibyl/nodes/summarize.py`

```python
async def summarize(state: MeetingState) -> dict:
```

1. Takes `state["transcript"]` (text only, no audio)
2. Sends to Gemini with a structured prompt requesting: overview, key topics, important details
3. Returns `{"summary": response.text}`

This node is text-in, text-out. It doesn't need the audio file.

---

## 9. Node: Extract Actions

**File:** `src/sibyl/nodes/extract.py`

```python
async def extract_actions(state: MeetingState) -> dict:
```

1. Takes `state["transcript"]`
2. Sends to Gemini with a prompt requesting JSON output
3. Parses the JSON response (with markdown fence stripping as fallback)
4. Returns `{"action_items": [...], "decisions": [...], "participants": [...]}`

**JSON parsing resilience:**
```python
text = response.text.strip()
if text.startswith("```"):           # Strip ```json ... ``` fences
    text = text.split("\n", 1)[1]
    text = text.rsplit("```", 1)[0]
data = json.loads(text)
```

Even though the prompt says "no markdown fences," models sometimes add them. This fallback handles it. If parsing still fails, empty lists are returned — the pipeline doesn't crash.

---

## 10. Node: Detect Emotions

**File:** `src/sibyl/nodes/emotion.py`

```python
async def detect_emotions(state: MeetingState) -> dict:
```

1. Reads the WAV file AND the transcript
2. Sends **both** to Gemini — audio as inline_data + transcript as text
3. Requests JSON with per-speaker emotion profiles
4. Returns `{"speaker_emotions": [...], "meeting_mood": "..."}`

**Why both audio and transcript?** The audio carries vocal cues (pitch, pace, tension, tremor) that text alone can't capture. But the transcript provides context — knowing *what* was being discussed when tone changed makes the emotional analysis more meaningful. By sending both, Gemini can correlate "voice got tense" with "was discussing missed deadlines."

**Each speaker_emotions entry:**
```json
{
  "speaker": "Joseph",
  "overall_tone": "calm, assertive",
  "emotions_detected": ["confident", "slightly impatient"],
  "notable_moments": ["Got direct when asking about the timeline"]
}
```

---

## 11. Shared Client: nodes/__init__.py

**File:** `src/sibyl/nodes/__init__.py`

```python
def get_client() -> genai.Client:
    api_key = os.environ.get("GOOGLE_AI_API_KEY", "")
    return genai.Client(api_key=api_key)
```

Every node calls `get_client()` to create a Gemini client. This centralizes the API key lookup. The `google-genai` SDK's default is to look for `GOOGLE_API_KEY`, but we use `GOOGLE_AI_API_KEY` because that's what's in the user's `.bashrc`.

**Why create a new client per call?** The `genai.Client` is lightweight — it doesn't hold connections open. Creating one per node call is simpler than managing a singleton across async contexts.

---

## 12. Configuration: config.py

**File:** `src/sibyl/config.py`

```python
@dataclass
class Config:
    gemini_api_key: str      # From GOOGLE_AI_API_KEY
    gemini_model: str        # Default: "gemini-2.5-flash"
    output_dir: Path         # Default: ~/.local/share/sibyl
    sample_rate: int         # Default: 16000
    channels: int            # Default: 1
    record_system_audio: bool # Default: True
    record_mic: bool         # Default: True
```

`__post_init__` creates the output directory if it doesn't exist. `load_config()` reads from environment variables with sensible defaults.

---

## 13. Logging: log.py

**File:** `src/sibyl/log.py`

```python
def get_logger(name: str) -> logging.Logger:
```

Creates loggers namespaced under `sibyl.*` (e.g., `sibyl.transcribe`). Writes to **stderr** so it doesn't interfere with stdout output. Controlled by `LOG_LEVEL` env var.

Each node creates its own logger:
```python
log = get_logger("transcribe")  # → sibyl.transcribe
log.info("Transcribing: %s", audio_path.name)
```

---

## 14. Data Flow Diagrams

### Batch mode: `record -p`

```
User runs: uv run sibyl record -p

cli.main()
  └── cmd_record(args)
        ├── load_config() → Config
        ├── record_meeting(config)
        │     ├── _get_default_monitor() → "speaker.monitor"
        │     ├── _get_default_source() → "mic1"
        │     ├── Thread 1: record from monitor → monitor_frames[]
        │     ├── Thread 2: record from mic → mic_frames[]
        │     ├── [Ctrl+C] → stop_event.set()
        │     ├── mix: monitor + mic → audio
        │     ├── sf.write("meeting_20260402.wav")
        │     └── return Path
        │
        └── asyncio.run(process_meeting(audio_path))
              └── graph.ainvoke({"audio_path": "..."})
                    │
                    ├── transcribe(state)
                    │     └── Gemini API: audio → transcript
                    │     └── return {"transcript": "..."}
                    │
                    ├── (parallel) ─┬── summarize(state)
                    │               │     └── return {"summary": "..."}
                    │               │
                    │               ├── extract_actions(state)
                    │               │     └── return {"action_items": [...], ...}
                    │               │
                    │               └── detect_emotions(state)
                    │                     └── return {"speaker_emotions": [...], ...}
                    │
                    └── merged final state → _print_results() → stdout + .json
```

### Live mode: `live -s`

```
User runs: uv run sibyl live -s

cli.main()
  └── cmd_live(args)
        ├── load_config() → Config
        ├── asyncio.run(live_transcribe(config))
        │     ├── resolve audio device
        │     ├── genai.Client.aio.live.connect(WebSocket)
        │     │     ├── send_audio task:
        │     │     │     sounddevice callback → PCM16 → audio_queue → session.send_realtime_input()
        │     │     ├── receive_transcription task:
        │     │     │     session.receive() → input_transcription.text → print to terminal
        │     │     └── [Ctrl+C] → stop_event → cancel tasks
        │     ├── sf.write("meeting_20260402.wav")
        │     └── return {"audio_path": "...", "transcript": "..."}
        │
        └── asyncio.run(process_meeting(result["audio_path"]))
              └── (same LangGraph pipeline as batch mode)
```

---

## 15. Key Concepts

### PipeWire Monitor Sources

On Linux, audio output devices (sinks) have companion "monitor" sources. A monitor captures whatever audio is being played through that sink. This is how we capture meeting audio without any special drivers:

```
[Zoom/Meet/Teams]
       │
       ▼
  [PipeWire Sink: Speaker]  ──→  [Your speakers]
       │
       └── [Monitor Source]  ──→  [Our recorder captures this]
```

### Gemini's Audio Understanding

Gemini doesn't use a separate speech-to-text model. The foundation model natively processes audio tokens alongside text tokens. This means:
- It understands tone, emotion, and speaker characteristics
- It can follow instructions about what to do with the audio
- It can correlate audio events with textual context

### LangGraph State Merging

When parallel nodes complete, LangGraph merges their return dicts into the state:
```python
# State before parallel step:
{"audio_path": "...", "transcript": "..."}

# summarize returns:     {"summary": "..."}
# extract returns:       {"action_items": [...], "decisions": [...], "participants": [...]}
# emotions returns:      {"speaker_emotions": [...], "meeting_mood": "..."}

# State after merge:
{
  "audio_path": "...",
  "transcript": "...",
  "summary": "...",
  "action_items": [...],
  "decisions": [...],
  "participants": [...],
  "speaker_emotions": [...],
  "meeting_mood": "...",
}
```

If two nodes write to the same key, the last one wins (undefined order). That's why each node writes to distinct keys.

### WebSocket vs REST

| | REST (batch nodes) | WebSocket (live mode) |
|---|---|---|
| Protocol | HTTP POST | WSS (persistent) |
| Connection | One request, one response | Long-lived bidirectional |
| Latency | Seconds (send full audio) | Milliseconds (stream chunks) |
| Model | gemini-2.5-flash | gemini-3.1-flash-live-preview |
| Input | Base64 WAV in JSON | Raw PCM16 bytes |
| Output | Complete text response | Streaming tokens |

### Audio Format Reference

| Parameter | Batch (REST) | Live (WebSocket) |
|---|---|---|
| Format | WAV (with headers) | Raw PCM (no headers) |
| Encoding | float32 → base64 | float32 → int16 LE |
| Sample rate | 16kHz | 16kHz |
| Channels | 1 (mono) | 1 (mono) |
| MIME type | audio/wav | audio/pcm;rate=16000 |

---

## 16. How to Extend

### Adding a new parallel node

1. Create `src/sibyl/nodes/my_node.py`:
```python
from sibyl.nodes import get_client
from sibyl.state import MeetingState

async def my_node(state: MeetingState) -> dict:
    # Use state["transcript"], state["audio_path"], etc.
    return {"my_field": result}
```

2. Add the field to `state.py`:
```python
class MeetingState(TypedDict, total=False):
    ...
    my_field: str  # or whatever type
```

3. Wire it into `graph.py`:
```python
from sibyl.nodes.my_node import my_node

graph.add_node("my_node", my_node)
graph.add_edge("transcribe", "my_node")
graph.add_edge(["summarize", "extract_actions", "detect_emotions", "my_node"], "__end__")
```

4. Add output to `cli.py` in `_print_results()`.

### Adding a sequential step after parallel nodes

```python
graph.add_node("final_step", final_step)
graph.add_edge(["summarize", "extract_actions", "detect_emotions"], "final_step")
graph.add_edge("final_step", "__end__")
```

This node would receive the full merged state from all parallel nodes.

### Adding checkpointing

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async def process_meeting(audio_path):
    async with AsyncSqliteSaver.from_conn_string("meetings.db") as checkpointer:
        app = build_graph().compile(checkpointer=checkpointer)
        result = await app.ainvoke(
            {"audio_path": str(audio_path)},
            config={"configurable": {"thread_id": "meeting-123"}},
        )
    return result
```

This saves state after each node, allowing you to resume from the last successful node if something fails.
