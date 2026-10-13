# Jev Linux

A native voice controller for Ubuntu. Local Moonshine English speech recognition
produces partial transcripts. Gemini 2.5 Flash plans the complete instruction;
TypeSafe's Jev chooses desktop actions for each step through OpenRouter. A clear
“open the notes app…” can launch Text Editor while the rest of the sentence is
still being spoken. Dictation, planning, and note contents wait for the final
transcript.

## Start

Complete [installation](#install-on-ubuntu) first, then open **Jev Linux**
from Ubuntu's app launcher or run this from the checkout directory:

```bash
./run.sh
```

On the first launch, open **Settings** and enter your OpenRouter API key.
Planning and writing also use this key. Keep the microphone off while checking
your settings, then try a typed command before starting voice capture.

1. Click **Start listening**. The microphone starts off on every launch.
2. Say a command. Your words and each action appear in the window.
3. For keyboard shortcuts or scrolling, focus the target app while listening.
4. Click **Allow desktop control**, then approve Ubuntu's sharing dialog when
   you need keyboard or scrolling input. Opening apps and native accessibility
   actions can work without this permission.
5. Click **Stop / cancel** to stop capture and discard queued decisions. Escape
   does the same while the Jev window has focus. These shortcuts are local to
   the Jev window; speaking “stop” also cancels queued actions.

You can use the command box without a microphone. **Settings** lets you change
or replace the API key, select direct TypeSafe instead of OpenRouter, and choose
another local speech model folder. The active model is shown above the microphone
button. **Moonshine Small Streaming** is installed by default. You can install
**Medium Streaming** as an optional model with higher memory use, then select it
in Settings.
**Break longer tasks into steps** is enabled by default; its planning model is
`google/gemini-2.5-flash`.

Task memory is on by default. Click **Memory** to review recent tasks and saved
preferences, or use **Settings** to pause memory. **Clear memory** deletes that
history and those preferences; existing notes stay in your Documents folder.

## Commands to try

- “Open the notes app and create a note containing hello world.”
- 'Create a note containing "Bring the printed agenda.", then open Calculator.'
- “Read https://example.com and create a note summarizing its main points.”
- 'Read that note, append "Confirm the meeting time." to it, and open it again.'
- “Read the visible text in the focused app and create a note summarizing it.”
- “Create a note about three things to prepare for tomorrow's meeting.”
- “Open Calculator.”
- “Open YouTube in Chrome, then open Calculator.”
- “Open Files.”
- “Open example dot com.”
- “Search the web for Ubuntu keyboard shortcuts.”
- “Click the new document button.”
- “Type hello world into the search box.”
- “Press control F.”
- “Scroll down.”
- “Remember that I prefer short notes.”
- “Open that note again.”
- “Open the same website again.”
- “Compose another similar note.”
- “Clear my memory.”

“Notes” maps to installed Text Editor when there is no dedicated notes app.
New notes are saved as unique Markdown files in `~/Documents/Jev Notes` and
opened in Text Editor. Existing files are never overwritten by note creation.
For “containing…” or quoted text, contents are copied verbatim. For “about…”
and composition requests, a separate OpenRouter text model writes the prose:
`google/gemini-2.5-flash-lite` by default. Jev itself selects decisions and does
not generate prose.

## Longer instructions and progress

The planner splits the whole command into ordered steps with expected outcomes.
Each step limits Jev's choices to the operation types that can accomplish it,
so unrelated actions do not compete with a clearly requested action. A named
browser is kept with the website request: “open YouTube in Chrome” opens that
address directly in installed Chrome. It does not silently substitute your
default browser when the requested browser is unavailable.
For a direct opening clause at the start of a command, plan validation preserves
that operation even when the page is already visible. Only a successful opening
in the current task can satisfy it; saved history cannot replace it with a focus step.
Jev works on one step at a time, rereading the available desktop controls and
using successful action results to check progress. **Current task** shows the
steps, completed count, and the reason for a pause. Opening an app or clicking
a button does not by itself establish that every later part of the goal is done.

By default, each run allows up to 60 actions and 300 seconds. Reaching either limit pauses
the task and keeps completed steps. Click **Continue task**, or say “continue
task” or “resume”, to work on the remaining steps in the current app session.
Continuing does not approve a sensitive action. A blocked step can also be
continued after you supply the missing detail or make its controls available.
Stop cancels the task; closing the app preserves memory but not a resumable live
plan.

When a step cannot advance, the controller retries reading the current state.
If a selected field changes while a page loads, Jev makes up to two fresh
decisions before input starts. It does not retry an input operation that has begun.
It may make one revised plan if that step has produced no effects, while keeping
completed effects in context. It pauses when completion cannot be verified,
asks for essential missing details, and reports unsupported parts. Finishing
the supported parts of a request does not mark its unsupported parts complete.
Answer a displayed task question through the command box or final spoken phrase;
the answer resolves the original task and keeps its completed effects. Cancel
first if you want to replace a question with a different task.

Local speech recognition gives a phrase 1.5 seconds of silence by default before
finalizing it, so short pauses stay in the same instruction. While a task is unfinished,
continuations beginning “And then…”, “also…”, “then…”, “after that…”, or “next…”
extend its existing goal. A new standalone command starts a new task.

Sensitive actions show an approval button; say “confirm” or click it to continue,
or cancel. For keyboard actions, keep the target app focused and say “confirm”;
clicking the Jev window would move keyboard focus. Keyboard shortcuts are bound
to the original application, window, and control; a changed target requires a
new command. The controller checks current targets before acting and tracks
completed actions to avoid repeating them within a task.

## Memory and follow-up instructions

Memory survives closing and restarting the app. It keeps the latest 30 tasks,
up to 60 performed actions per task and their note/website references, and up to
20 preferences that you explicitly dictate with “Remember that …” or
“Remember this: …”.
It records completion, cancellation, failures and requests for more details.
Interrupted work is context for a follow-up; the app never resumes it by itself.
Earlier partial transcripts are not saved as a stream of hypotheses.

Follow-ups can refer to recent tasks: “open that note again”, “open the same
website”, or “compose another similar note”. Notes are reopened only from your
configured Jev Notes folder. When drafting a similar note, the writer can use
up to 8,000 characters from the previous note. “Compose another similar note”
creates a new note. An explicit append request can add text to a remembered note
without replacing its existing contents. The app checks the note's revision
before appending and refuses if it changed during the operation; external
editors may not honor its file lock. Replacing a whole existing document is not
supported. Preferences help select available actions, and your current
instruction takes priority.

**Memory** shows history and preferences without sending a request to the AI.
**Clear memory** or “Clear my memory” stops queued work and deletes saved memory.
It keeps files you created. Turning memory off in Settings pauses saving and
using context; retained history stays reviewable and clearable, including after
a restart. If local storage fails, the app reports it and can run commands
without saved context.

## What is supported

Installed application launches, accessible button/menu activation, text insertion,
keyboard shortcuts, scrolling, opening websites, web searches, and saved notes.
Tasks can read a remembered Jev note, append text to it, read a public web page,
or read the focused app's visible accessibility text for a later summary or
extraction. These read operations have text-size limits. Web reading uses HTTP/HTTPS without browser
cookies, login, or page scripts; pages that need those may require manual help.
The app reads Linux's AT-SPI accessibility tree. Apps with inaccessible controls,
canvas-only interfaces, hidden/off-screen controls, and ambiguous focus can require
manual help. This is a prototype, and it cannot reliably automate every app.
It does not interpret screenshots, run a generated shell script, or perform
unbounded background tasks.

Early execution is limited to opening clearly named apps. Other actions wait
until the speech endpoint. Recognition quality and network latency affect speed.
The Moonshine model is configured for English, uses native CPU streaming, and
keeps revising partial transcripts until a final phrase is ready. Short pauses
are combined with the 1.5-second silence grace period. App names can still be
misheard, and a longer pause can finish a phrase before your whole request is
spoken.

On this laptop, Small Streaming used approximately 492 MiB of memory; Medium
Streaming used 812 MiB. Both transcribed the public 11-second test clip correctly
and kept up with its audio. These sample checks do not measure accuracy on your
own accent or microphone. Small leaves more memory available for desktop apps.
The runtime only loads downloaded local model files and never uploads audio.

## Privacy and credentials

Audio is recognized locally and is not saved to disk or sent to Jev. Your command,
visible accessibility labels, short text values, and recent action results go to
the selected decision provider. With planning enabled, the command, available
desktop context, and relevant recent task context also go to the OpenRouter
planner. Requested source text from notes, public pages, or the focused app can
go to Jev and the configured OpenRouter writer for later steps and summaries.
Password controls are excluded. Other on-screen and source text may be sensitive;
use the controller only with text you want to share with these providers.

When memory is enabled, task history and explicit preferences are stored in
`~/.local/share/jev-linux/memory.sqlite3` with mode `0600` in a private `0700`
folder. This is local storage with file permissions, not encryption. Recent
context and preferences can go to the decision provider, OpenRouter planner,
and prose writer for follow-ups. Memory excludes microphone audio, screenshots,
accessibility control values and identifiers, and configured
API keys. Commands and dictated text may themselves contain private information.

API keys live outside this project in `~/.config/jev-linux/config.json` with file
mode `0600` and a `0700` parent folder. This is a private local file, not encrypted
keychain storage. The Settings fields never reveal the stored key. Environment
variables `OPENROUTER_API_KEY` and `TYPESAFE_API_KEY` are also supported. To
replace an exposed key, revoke it through your provider and paste its
replacement into Settings.

No microphone autostart or boot service is installed. Ubuntu's desktop sharing
permission lasts for the running session and can be withdrawn through GNOME.

## Supported environment

Jev Linux is a prototype for Ubuntu's GNOME desktop with AT-SPI accessibility
and the GNOME RemoteDesktop portal. Live workflows have been checked on
Ubuntu 25.10 with Python 3.13.7. Other Ubuntu releases, desktops, and Python
versions may need additional testing. English speech recognition is supported.
Automatic refresh of edited notes currently expects GNOME Text Editor's English
interface; a translated or changed interface can require a manual reload.

The automated test workflow targets Ubuntu 24.04 using its system Python and GI
bindings. These headless tests do not establish microphone accuracy or reliable
automation of every application on a fresh desktop.

## Install on Ubuntu

Clone the repository into a directory named `jev-linux`, then enter it:

```bash
git clone https://github.com/musagithub1/Browser_use.git jev-linux
cd jev-linux
```

You can also download the repository ZIP, extract it, and open a terminal in the
directory containing `run.sh`. Install the Ubuntu desktop dependencies, then
run setup:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0 alsa-utils python3-venv
./scripts/setup.sh
./run.sh --doctor
./run.sh
```

Setup uses a virtual environment with Ubuntu's system GI bindings. It downloads
the official Moonshine Small Streaming English model, verifies its file sizes and
checksums, installs the app launcher, and adds the `jev-linux` command to
`~/.local/bin`. Model downloads resume from the local model folder.
Rerunning setup reuses an existing valid virtual environment and preserves
your selected speech settings. Use `scripts/setup_speech.py --model small` or
`--model medium` explicitly when you want to change that selection.
Native entry points remove incompatible GTK and settings paths inherited from
Snap editors.
Set `JEV_AUDIO_DEVICE` if you need an ALSA input device other than `default`.

To install or switch the local speech model independently:

```bash
.venv/bin/python scripts/setup_speech.py --model small
.venv/bin/python scripts/setup_speech.py --model medium
```

Restart Jev Linux after using the installer. Medium can improve accuracy at the
cost of memory; Small is the recommended default on this laptop. Existing Vosk
model files are preserved as an optional fallback in Settings. On a fresh
installation, run `scripts/download_model.py` first if you want that fallback.

## Checks and troubleshooting

```bash
./run.sh --doctor
./run.sh --check-api
./run.sh --plan 'Open the notes app and create a note containing hello world'
./scripts/test.sh
```

The test runner cleans inherited Snap editor paths before importing GTK/GI and
runs the local unittest suite without API keys, microphone capture, or speech
model downloads. Pass unittest arguments to run a smaller check, for example
`./scripts/test.sh discover -s tests -p test_planning.py -v`.

For a live desktop check, `.venv/bin/python scripts/check_workflow.py --execute`
uses paid API calls, creates private scratch notes under `~/.cache/jev-linux/checks`,
opens Text Editor and Calculator, and checks a create/append workflow. Add `--web`
to check reading example.com and writing a summary. This check leaves task memory
and your Documents notes untouched, and does not start microphone capture.

`--doctor` reads capabilities without starting the mic. `--check-api` makes a
small paid decision request. With planning enabled, `--plan` prints the full
structured plan, including expected outcomes and unsupported parts, without
executing it. It sends the command, current accessibility context, and relevant
memory to the OpenRouter planner. With planning disabled, it previews Jev's
next-action decision instead.

Normal app use makes paid API calls for changing transcripts and step decisions.
Planning, a revised plan, and prose writing add paid OpenRouter calls; a long
instruction can require several requests. Speech recognition stays local, while
planning, decisions, and writing require internet access and provider credit.
Direct TypeSafe can supply Jev decisions, but the planner and prose writer still
use OpenRouter and its API key.

Chrome and other Chromium browsers are detected through their native
accessibility interface. Jev requests their control tree at runtime, then scopes
the next snapshot to the requested app so a large unrelated window cannot hide
its fields. Follow-up commands retain the last app and website within the session,
including when saved memory is off; control IDs are always read again.

For example, say **“Open YouTube in Google Chrome”**, then **“Type Hello in the
search bar.”** Jev distinguishes a page's search field from the browser address
bar. It can reveal a collapsed page search box through its accessible Search
button, then insert **Hello**. Revealing the field alone does not complete the
typing step, and a visible page field excludes the Search submit button from
that step's choices. If the field requires keyboard control, the task
waits for Ubuntu's desktop-control permission dialog and continues from the
unfinished step after approval. Denial leaves it waiting; **Allow desktop
control** retries. On GNOME, field typing can bring a background app forward
using its configured favorite-app shortcut. Jev verifies the exact window and
field before typing. If the shortcut is unavailable or a different window of
the same app is already in front, bring the intended window forward and continue.
An ambiguous foreground app blocks global input.
If microphone capture fails, check **Ubuntu Settings → Sound → Input** and the
chosen ALSA device. The app stays open on provider errors and shows the cause.

## Implementation

- `moonshine_voice.py`: native English streaming, microphone capture, phrase aggregation, and cancellation.
- `voice.py`: speech engine selection and the optional original Vosk fallback.
- `controller.py`: debounce, partial/final policy, closed choices, cancellation,
  confidence checks, confirmation, and action execution.
- `planning.py`: structured whole-command plans through the OpenRouter planner.
- `task_runner.py`: ordered steps, outcome checks, bounded retries, safe replanning,
  and session-local continuation.
- `notes.py`: unique note creation, verified reads, and revision-checked appends.
- `web_reader.py`: bounded public page text without browser login or scripts.
- `memory.py`: private SQLite task history, explicit preferences, bounded retention, and clearing.
- `provider.py`: persistent HTTP connections, validated Jev answers, optional prose.
- `desktop.py`: installed apps and live AT-SPI controls.
- `gnome_activation.py`: resolves existing GNOME favorite-app shortcuts without
  changing settings or launching unrelated apps.
- `main_thread_desktop.py`: keeps native desktop objects on GTK's thread and
  cancels queued actions when their task changes or the app closes.
- `portal.py`: GNOME Wayland keyboard/pointer consent and input.
- `ui.py`: GTK window, current task progress, memory review, and Settings.

OpenRouter Jev uses `POST /api/alpha/decisions` with `typesafe/jev-1.13`, rather
than its regular chat endpoint. Speech streaming means repeated decisions on
partial transcripts, not model output streaming.

References: [OpenRouter Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request),
[TypeSafe Jev](https://docs.typesafe.ai/introduction),
[Moonshine Voice](https://github.com/moonshine-ai/moonshine),
[RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html).

## License

Jev Linux's project source is licensed under the [MIT License](LICENSE).
Third-party dependencies, downloaded speech models, and hosted services retain
their own licenses and terms. Adapted agent-configuration templates are
attributed in [Third-party notices](THIRD_PARTY_NOTICES.md).
