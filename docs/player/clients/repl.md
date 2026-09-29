# The REPL client

The REPL is a compact terminal client: a scrolling log of output above a single command
line you type into. It is lighter than the panel-based Textual TUI but built on the same
toolkit, so it gets rich text, clickable targets, and good keyboard/accessibility support.

Like the TUI it can host a world in this process with no network port or drive a running
server over HTTP through the web controller. The header keeps a live status line: who you
are playing, the connection, your action/focus points, and the world clock.

## Launch

For a first local game with local Ollama and the Apple Crossing tutorial:

```bash
ollama pull qwen3.5:9b  # one-time model download
scripts/quickstart-repl
```

Host a local world:

```bash
uv run --all-extras bunnyland repl
```

Connect to a running server:

```bash
uv run --all-extras bunnyland repl --server http://localhost:8765
```

For a server that requires player authentication, sign in from the REPL with `/login`.
The username and password are collected in a secure modal, and the password is masked and
never added to command history. The REPL opens the same modal automatically when the server
reports that authentication is required. You can also provide credentials when launching:

```bash
uv run --all-extras bunnyland repl \
  --server https://play.example/v1 \
  --username player \
  --token-file ~/.config/bunnyland/player-token
```

Without `--password-stdin`, the launch command prompts for the password before opening the
REPL. `--token-file` is optional; when provided, the bearer token is stored in that explicit
mode-0600 file and reused on the next launch.

List the demo worlds and generators you can pass to `--generator`:

```bash
uv run --all-extras bunnyland repl --list-generators
```

Useful options mirror the TUI: `--seed`, `--generator`, `--claim-fallback`, and
`--claim-timeout-minutes`. It is a full-screen terminal app, so run it in a real terminal,
not through a pipe. Before loading a local plugin world or joining a flagged remote world,
the REPL requires you to accept its content warning. See
[Content warnings](README.md#content-warnings) for saved and command-line ignore options.
It then shows the world's title and welcome or message-of-the-day text. The introduction
screen can remember a skip for that world/server pair or for every world/server; both
choices are saved in the shared terminal configuration and do not bypass content warnings.

The first local launch also opens the shared terminal chat setup. It saves only the
provider, model, endpoints, and enabled state in
`$XDG_CONFIG_HOME/bunnyland/terminal.yml`; credentials remain in environment variables.
Remote sessions skip local provider setup. See [Terminal character chat](chat.md).

## Pick a player

List the characters in the world, then take control of one:

```text
> who
  Juniper
  Pib
> play Pib
You are now Pib.
```

Names resolve case-insensitively and by prefix (`play Pi` finds `Pib`), the same way the
server resolves names you type in commands.

## Two ways to give a command

Named commands use canonical `command parameter=value` syntax:

```text
> move direction=north
> take item_id=a brass key
> say text=Hello, burrow.
```

Natural commands use the same concise phrasing the Discord and agent clients accept:

```text
> go north
> take brass key
> say Hello, burrow.
```

Multi-word values are fine in both forms. Entity names are resolved against what your
character can currently reach. If a name does not match, the REPL says so and suggests
nearby names instead of submitting a doomed command.

## Click a target

Characters, items, rooms, containers, and exits in the output are shown as highlighted
links. Click one to drop its name into the command line at the cursor:

```text
> take           (then click "a brass key" in the log)
> take a brass key
```

## Tab completion

Press Tab to complete, in order:

- a command at the start of the line (`mo` -> `move`);
- a parameter name after the command (`move ` -> `direction=`, `exit_id=`);
- a parameter value after `=`: reachable entity names for entity parameters, or the
  enumerated choices for others.

When several completions are possible, the line fills in as far as they agree and the
choices are listed in the log. `help` completes command names; `play`, `sheet`, `profile`,
and `chat` complete character names.

## Character sheets and chat

Open the current player's native sheet, or name another character from the lobby:

```text
> sheet
> profile Marlow
```

The command emits a UI intent rather than printing a browser URL, so the REPL opens the
same scrollable sheet used by the panel TUI. Start a conversation the same way:

```text
> chat Marlow
```

The conversation opens as a Textual screen over the REPL. Closing it returns to the command
log. Replies, pending actions, tool results, and provider failures are shown in the screen
without blocking refreshes or command history.

## Live narration

The log surfaces things happening around you as they occur: speech, movement, and other
activity your character can perceive in its current room. Room-scoped events from
elsewhere are not shown. High-frequency bookkeeping is suppressed to keep the feed
readable.

When you address an autonomous character by name, the log also shows a short reaction
summary if that character's action has no visible world event, such as waiting or having
an action rejected. This confirms that the character responded without exposing its
private prompt or model reasoning.

After your character moves through an exit, the REPL shows the destination-room summary so
you can immediately decide what to do next.

If a `--server` connection drops, the failure is reported once and a reconnect is noted
when it recovers, rather than repeating every second. While reconnecting, the REPL uses the
claim-scoped recent-event feed and deduplicates entries already received from the live
stream.

Event fact lines follow progressive disclosure: important changes appear in the live log,
while calm/default component state remains available through `look`, inspection, or a
detailed character view instead of being repeated every turn.

## History

Up/Down walk previous commands, and history is saved to
`~/.config/bunnyland/repl-history` (honoring `XDG_CONFIG_HOME`) so it survives restarts.

## Meta commands

| Command | Effect |
|---------|--------|
| `/login` | securely sign in to a remote server, or sign in again |
| `look` | show your current room, occupants, exits, and inventory |
| `inventory` (`inv`) | show what you carry, grouped into worn, held, and carried |
| `who` | list the characters you can play |
| `points` | show your action/focus points |
| `play <name>` | take control of a character |
| `help [command]` | list commands, or show one command's parameters |
| `refresh` | refresh your view now |
| `sheet [name]` / `profile [name]` | open a native character sheet |
| `chat <name>` | open a character conversation |
| `quit` / `exit` | leave; Ctrl-C also quits |

## Example session

```text
> play Pib
You are now Pib.
> look
Parlor
  Pib (you)
  Marlow
  an apple
  exits: north -> Hallway
  carrying: a brass key
> take apple
> go north
> quit
```
