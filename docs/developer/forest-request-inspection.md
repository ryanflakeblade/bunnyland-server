# Inspect forest model requests offline

`tests/test_forest_request_capture.py` runs the authored forest generator, player-turn
dispatch, prompt builder, character history, and OpenRouter SDK serialization. An HTTPX
mock transport intercepts every provider request and returns a fixed `say` tool call.
It uses a placeholder key and an `.invalid` endpoint; no model service is contacted.

With the project's test and LLM dependencies already installed, run from the repository
root in PowerShell:

```powershell
$env:BUNNYLAND_REQUEST_DEMO_DIR = "$PWD/artifacts/forest-request-demo"
uv run --no-sync -m pytest tests/test_forest_request_capture.py -q --basetemp artifacts/pytest-forest-request-demo
Remove-Item Env:BUNNYLAND_REQUEST_DEMO_DIR
```

The test starts with Lu Zhishen already in the clearing, then submits:

1. `say text=林冲，嫂嫂怎样了？`
2. `say text=林冲，你想让我怎么帮你？`
3. `say text=董超，你们在这里做什么？`

The output directory contains a Chinese `README.md`, complete `request-01.json` through
`request-03.json` HTTP request bodies, and readable Markdown versions of all messages
and tool definitions. Each run overwrites these seven demo files. Without the environment
variable, pytest writes them into its temporary test directory instead.

The test checks that the first Lin Chong request has two messages, his follow-up has five,
and Dong Chao starts with two. It also checks character secrets stay out of other
characters' messages, and idle ticks and AI speech do not trigger extra calls. Public
speech may appear in another character's observed events; that is distinct from sharing
private history.

These are new requests from the current code, not reconstructed historical requests.
The assistant replies are explicitly marked test fixtures. JSON character counts are
not token counts, and this test does not measure billing, model quality, or every possible
information boundary. Full request bodies exclude HTTP headers, including Authorization.
