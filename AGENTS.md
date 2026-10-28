# AGENTS.md: working on GameFrame

This file is for agents and people who change **GameFrame itself**. To reverse engineer a
game *with* it, create a project (`scripts/new-project.sh`): the project gets its own `AGENTS.md`
from `templates/project/`, and the skills in `skills/` (start with `re-start`).

## What this repository is

This fork's PS2 experiment follows GameFrame's behavioral verification strategy.
Use `docs/consoles/ps2-functions.md`; compiler byte matching is not an acceptance
requirement. Keep game-specific source and ground truth in the private game's
repository or external data directory. Unsupported execution is not a passing test.

GameFrame is an agentic framework for reverse engineering games. Its playbook of skills and agents runs inside your coding agent. Its toolkit of tested tools checks every result against the original. The playbook is the method, the skills in `skills/re-*`, the
agents in `agents/` and these rules. The toolkit is `tools/` and `scripts/`. Nothing here is
specific to one game. Un-Banished (https://github.com/tanepiper/unbanished) is the worked case study and may
be mentioned and linked, but examples and advice must stay general.

## The user decides

This applies to every skill, tool and document in this repository, and to every project made
with it. An agent is not the judge of the user's intentions, of which law applies to the user,
or of what that law means. If the user wants to do something, help them do it. You may advise.
When you know something that bears on the choice, such as a law, a risk or a project default,
say it once, briefly, with its source. Then follow the user's decision.

The rules in the skills and in a project's `AGENTS.md` are defaults that keep the work exact and
publishable. The user may change any of them for their own project. Write new text in this
repository the same way: give facts and advice, and leave the decision to the user.

## Rules

These rules are for changes to GameFrame itself, which is a public repository.

1. **No game material, ever.** No game binaries, data, assets, saves, decompiled code or
   disassembly from any commercial game. The only binaries the tests use are built from
   `examples/sample-game/` and `examples/dos-sample/` (our own code) into their ignored
   `build/` folders. The PS2 backend also uses ELF files built at test time from
   hand-authored synthetic instructions in `tests/test_ps2_emu.py`.
   Screenshots are the exception and are always fine: of the
   original games and of the case-study ports, in `docs/assets/case-studies/`, compressed
   WebP, where a page needs them.
2. **Every tool is tested.** A new or changed tool gets a check in `tests/run.sh` against the
   sample game, and `tests/run.sh --ghidra` passes before you commit.
3. **Every tool documents itself.** A module docstring or header comment with usage, inputs,
   outputs and what it does not do; `--help` or no arguments prints it.
4. **Skills are self-contained.** An agent that loads one skill must be able to do that step
   from it alone: exact commands, decisions, evidence to produce, pitfalls. Keep the skills,
   the agents and their generated copies (`gameframe agents --emit all`), the README tables,
   `templates/project/AGENTS.md` and the plugin's version in `.claude-plugin/plugin.json`
   consistent when anything changes.
5. **Plain English.** Short sentences, common words, one idea each: the skills are read by
   agents and translated by people.
6. **Files stay small.** Split a tool before it passes a few hundred lines: a package with one
   module per concern (`tools/emu/dos_env/`: DOS, DPMI, BIOS, PC). Generated code is split too
   (`tdinfo.py rust-globals` writes one file per Pascal unit). Keep import paths stable.
7. **Dependencies stay small.** Python tools use the standard library plus `requirements.txt`
   (unicorn, pefile, capstone). Shell scripts use bash and the tools `scripts/setup.sh` installs.
8. **Improve GameFrame from real games.** When a game project hits something generic (a
   tool crash, a missing format, a pitfall), fix it here with a test against our own samples. Keep the game-specific part in the game's repository.
   This checkout is the only GameFrame on the machine. The GameFrame home (`~/gameframe`, outside
   this clone) names it with `GAMEFRAME_ROOT` in `config.env`, and a project's `.env` may pin
   another, and a game's session changes GameFrame in a git worktree of
   it, never in a second clone ([docs/working/environments.md](docs/working/environments.md)).
9. **Two licences.** Code is Apache 2.0 (`LICENSE-APACHE`); documentation, skills and other
   prose are CC0 1.0, dedicated to the public domain (`LICENSE-CC0-1.0`). A new file takes the licence of its kind; the
   README's Licence section lists which folders are which.
10. **Stage only your own files** (`git add path`), never `git add -A`. Push after each verified change.

## Layout

| Path | What |
|---|---|
| `skills/re-*/SKILL.md` | One skill per step of the method, in the Agent Skills format. `.claude/skills` and `.agents/skills` link here, so every agent finds the one copy |
| `agents/re-*.md` | One agent per stage and twelve task agents, which hand off to each other (`.claude/agents` links here). `gameframe agents --emit all` writes their copies for Codex, Gemini CLI, Copilot and OpenCode (`.codex/agents`, `.gemini/agents`, `.github/agents`, `.opencode/agents`); never edit those |
| `.claude/settings.json`, `scripts/hooks/gate.py`, `scripts/hooks/gates/`, `scripts/hooks/git/pre-commit`, `hooks/hooks.json`, `.codex/hooks.json`, `.cursor/hooks.json`, `.github/hooks/gameframe.json`, `hooks/gemini-settings.json` | Permissions and the gates: the git pre-commit hook, and hooks for Claude Code, Codex, Gemini CLI, Cursor and Copilot: [docs/working/gates.md](docs/working/gates.md) |
| `.claude-plugin/` | The Claude Code plugin and its marketplace: the skills and the agents |
| `gameframe`, `tools/gameframe/` | The help and discovery command. It reads the skills and each tool's own help, so a new tool needs no entry there. `home.py` and `games.py` are the GameFrame home (`gameframe home`, `gameframe game`) |
| `scripts/` | setup, new-project, update-project, ghidra-export, ghidra-run, `clients.py` (the files each coding agent needs in a project), `release.py` (cut a release, then open the next `-dev` version), and `lib.sh` (paths and `.env`) |
| `tools/disc`, `tools/pe`, `tools/dos`, `tools/triage`, `tools/emu`, `tools/formats`, `tools/verify`, `tools/ghidra`, `tools/install` | The tools. `tools/install` is `gameframe install`, which installs a project's release build from its `install.json` |
| `templates/project/` | What `new-project.sh` writes into a game's repository |
| `templates/library-page/` | The plain HTML library page for browser ports, which works with `crates/library`, and its ready-made themes (`themes/`) |
| `launcher/` | gameframe-launcher: the frame in front of a game described by `frame.json` (a Wine kernel, the original's program). Its own Cargo workspace, not built by the tests |
| `examples/frame-demo/` | The frame with no game, for the browser and the desktop: its own Cargo workspace, not built by the tests |
| `examples/sample-game/` | The test target: C sources, build script, ground truth, a port, a decoder |
| `examples/dos-sample/` | The 16-bit test target: assembly, our own NE/MZ linker, Borland-style debug symbols |
| `tests/run.sh` | The checks |
| `docs/` | The taxonomy of games (`games/`), engines (Flash and Shockwave too), hardware, consoles, observing the original (`observe/`), media and files (`media/`), working as an agent (`working/`), case studies, ABI, DOS, game patterns, compiled code, legal notes, setup |
| `crates/` | A Cargo workspace of clean implementations taken from the case studies (OPL2, WMA, MSVC CRT, Turbo Pascal); rules in `docs/crates/README.md`. `cargo test --release` in `crates/` |
| `wrappers/` | A Cargo workspace of replacement DLLs for 1997 3D cards and DirectDraw (Glide 2, ATI3DCIF, S3D Toolkit, SGL's scene graph, DirectDraw), taken from the POD case study. Built for 32-bit Windows through `scripts/zcc.sh`; host tests in `tests/run.sh` |
| `CHANGELOG.md` | Every release, newest first. Add each change under Unreleased. `python3 scripts/release.py X.Y.Z` makes a release: it moves them under its version, sets the same version in `.claude-plugin/plugin.json` (the tests check the two match), tags the commit `vX.Y.Z`, then opens the next minor version as `-dev` with an empty Unreleased section. The commit gate blocks other commits on a tagged release until that is done (`release.py next`) |
| `GAPS.md` | Topics missing from the guides and tools, and where each is now covered. Tick an item when it is merged; add new gaps to it |
| `site/` | The documentation website (VitePress). It builds from `docs/`, the skills and the examples where they are; it holds no copies of their content |
| `.mcp.json` | The MCP servers (REA, DOSBox-X) for Claude Code. `gameframe mcp --emit all` writes them into `.codex/config.toml`, `.gemini/settings.json`, `.cursor/mcp.json`, `.vscode/mcp.json` and `opencode.json` |
