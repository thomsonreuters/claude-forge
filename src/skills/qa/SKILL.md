---
name: forge:qa
description: Full Forge QA checklist in Docker container. Use for release validation or comprehensive verification of all Forge features.
disable-model-invocation: true
argument-hint: '[--provider-profile openrouter|remote-litellm] [--from X.Y] [--to X.Y] [--reset] [--stop] [--keep] [categories...]'
allowed-tools: Read, Bash, Glob  # AskUserQuestion deliberately omitted — listing it triggers CC auto-approve bug (github.com/anthropics/claude-code/issues/29547). The tool remains available; omitting it preserves the interactive dialog.
---

# Full QA

Full Forge QA checklist inside a Docker container. The container IS the sandbox -- any command inside it is safe.

## Usage

```
/forge:qa                          Run full QA checklist
/forge:qa session proxy            Run specific categories only
/forge:qa --from 4.1               Resume from section 4.1
/forge:qa --from 4.1 --to 7        Run sections 4.1 through 6.x (excludes 7)
/forge:qa --from 10 --to 13        Run sections 10 through 12 (13 is excluded)
/forge:qa --provider-profile remote-litellm
                                   Use remote/shared LiteLLM instead of default OpenRouter
/forge:qa --reset                Kill container, remove image, rebuild from scratch
/forge:qa --stop                   Stop and remove the QA container
/forge:qa --keep                   Keep container running after completion
```

## Arguments

| Argument                                        | Description                                                                                                                                                                                                                                 |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--from X.Y`                                    | Resume from section X subsection Y.                                                                                                                                                                                                         |
| `--to X.Y`                                      | Stop before section X subsection Y (exclusive). Example: `--from 10 --to 13` runs sections 10-12 and stops before 13.                                                                                                                       |
| `--provider-profile openrouter\|remote-litellm` | Select the proxy backend family used by provider-dependent QA steps. Defaults to `openrouter`; `remote-litellm` is for shared/internal LiteLLM infrastructure.                                                                              |
| `--reset`                                       | Kill container, remove image, rebuild from scratch. Use when auto-staleness detection is insufficient: Dockerfile changes, Claude Code version upgrades, corrupt image layers, or persistent container state not cleared by workspace init. |
| `--stop`                                        | Stop and remove the QA Docker container.                                                                                                                                                                                                    |
| `--keep`                                        | Keep the container running after completion.                                                                                                                                                                                                |
| `categories`                                    | One or more category names to run (see allowlist below).                                                                                                                                                                                    |

## Execution

Follow these steps in order. Do not skip steps.

### Step 1: Parse Arguments and Route

Parse `$ARGUMENTS` to extract flags: `--provider-profile <profile>`, `--from X.Y`, `--to X.Y`, `--reset`, `--stop`,
`--keep`. Any remaining words after flags are category names. Default `--provider-profile` to `openrouter`. Valid
provider profiles are `openrouter` and `remote-litellm`; reject any other value before starting the container.

**Greet the user:**

"Running the full Forge QA checklist inside a Docker container. This requires Docker Desktop to be running. I'll walk
through each test section, run commands inside the container, and check assertions. Forge debug logging is enabled by
default in the container, and the run artifacts will include command output plus copied Forge logs. You can ask
questions or explore at any point."

### Step 2: QA Mode

Full QA runs the checklist inside a Docker container. The container IS the sandbox -- the agent can run any command
inside it safely.

**Execution model**: Run ONLY commands that appear in the checklist's bash blocks. Do NOT invent commands. Adaptability
is at the assertion/interpretation layer -- judge output against assertion text even if format changes. Keep command
execution deterministic.

**Set the scripts directory** from the skill's own location:

```bash
SCRIPTS="${CLAUDE_SKILL_DIR}/scripts"
```

**If `--stop` was set**: Run `bash "$SCRIPTS/start-container.sh" --stop` and stop. No tests.

**If `--reset` was set**: Pass `--reset` to `start-container.sh` in Phase 1 (it kills the container, removes the image,
and rebuilds from scratch). Continue with the normal flow after that. The script's auto-staleness detection (comparing
the image's git rev label to `HEAD`) handles most cases automatically; `--reset` is the manual escape hatch for
situations where the label matches but the image is wrong (see the `--reset` argument description above).

**Provider profile**: Pass the selected provider profile to `start-container.sh`. The script validates required
credentials and exports the QA template/proxy variables into the container environment. If a running container was
created with a different provider profile, `start-container.sh` fails with a reset/stop hint; surface that message and
stop.

**Category name allowlist** (exact match only -- reject unknown names):

| Name       | Section | Name        | Section |
| ---------- | ------- | ----------- | ------- |
| enable     | 0       | status-line | 8       |
| preflight  | 1       | commands    | 9       |
| extensions | 2       | resume      | 10      |
| auth       | 3       | config      | 11      |
| proxy      | 4       | search      | 12      |
| session    | 5       | guard       | 13      |
| hooks      | 6       | workflow    | 14      |
| costs      | 7       | skills      | 15      |
|            |         | handoff     | 16      |
|            |         | info        | 17      |
|            |         | disable     | 18      |
|            |         | uninstall   | 19      |
|            |         | cleanup     | 20      |

If category names were given, validate each against this allowlist. Reject unknown names: "Unknown category 'foo'. Valid
categories: enable, preflight, extensions, ..."

#### Phase 1: Start Container

Run `start-container.sh` to get a Docker container:

```bash
# Pass --reset if the user requested a full image rebuild.
# PROVIDER_PROFILE is the parsed --provider-profile value, defaulting to openrouter.
CONTAINER=$(bash "$SCRIPTS/start-container.sh" --provider-profile "$PROVIDER_PROFILE" ${REBUILD:+--reset})

# `start-container.sh` prints the container name on stdout
if [ -z "$CONTAINER" ]; then
  echo "ERROR: start-container.sh returned empty container name."
  exit 1
fi
```

Note: `start-container.sh` mounts a host state directory into the container at `/workspace/.forge/qa/`, so state
persists on the host at `${FORGE_HOME:-$HOME/.forge}/manual-testing/qa/`.

If it fails, show the error and stop. The script handles image build, staleness detection, container reuse, workspace
init, and jq preflight.

Tell the user: "Docker container ready: `<container>`. Starting QA run."

**Check for stale artifacts**: Probe the container for leftover state from a previous QA run.

Note: a freshly rebuilt container always has `/root/.claude/settings.json` seeded to `{}` by `start-container.sh`. Treat
that empty baseline file as clean, not stale.

```bash
docker exec "$CONTAINER" bash -lc 'test -d ~/.forge/proxies || test -f ~/.forge/installed.json || jq -e '\''type == "object" and length > 0'\'' ~/.claude/settings.json >/dev/null 2>&1' && echo "STALE" || echo "CLEAN"
```

If `STALE`: use AskUserQuestion to ask "Previous QA artifacts detected in container. Reset to clean state?" with options
"Reset" / "Keep (resume where left off)". If the user chooses Reset, stop and recreate the container, then continue from
Phase 1 with the fresh container. Do **not** try to scrub the live container in place: stale state can live in both
`/root` and `/workspace`, and the workspace reset must restore the seeded test repo.

```bash
bash "$SCRIPTS/start-container.sh" --stop
CONTAINER=$(bash "$SCRIPTS/start-container.sh" --provider-profile "$PROVIDER_PROFILE" ${REBUILD:+--reset})

if [ -z "$CONTAINER" ]; then
  echo "ERROR: start-container.sh returned empty container name after reset."
  exit 1
fi
```

This is more reliable than ad-hoc `rm -rf` cleanup because `start-container.sh` already owns workspace initialization.

#### Phase 2: Initialize State + Infra Probes

**Set the checklist index** from the skill's own location:

```bash
CHECKLIST="${CLAUDE_SKILL_DIR}/resources/checklist.md"
```

**Resolve the host-side state directory** (the mount makes host and container paths equivalent):

```bash
STATE_DIR_RAW="${FORGE_HOME:-$HOME/.forge}/manual-testing/qa"
STATE_DIR=$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(os.path.expandvars(sys.argv[1]))))' "$STATE_DIR_RAW")
STATE_FILE="$STATE_DIR/state.json"
```

**Prepare mounted artifact directories**. Raw step logs and pre-clean log snapshots live under the mounted QA state
directory; Forge's own debug logs live under `/root/.forge/logs` inside the container and are copied out later.

```bash
docker exec "$CONTAINER" mkdir -p /workspace/.forge/qa/logs /workspace/.forge/qa/forge-logs-snapshots
```

**Fresh run**: clear any previous run-local logs/snapshots, reset container debug logs, then initialize progress
tracking via `walkthrough-state.py`:

```bash
rm -rf "$STATE_DIR/logs" "$STATE_DIR/forge-logs-snapshots"
docker exec "$CONTAINER" bash -lc 'rm -rf /root/.forge/logs && mkdir -p /workspace/.forge/qa/logs /workspace/.forge/qa/forge-logs-snapshots'
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" init --force --mode full-qa "$STATE_FILE"
```

This creates the state file with schema version, checklist hash, and empty step records. The script handles all
bookkeeping -- the agent never constructs state JSON manually.

**Run infrastructure probes.** These drive `<!-- requires: X -->` skip decisions for the entire run:

| Probe     | Command                                                                                                                                                                                                                           | Stored as       | Meaning                                       |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- | --------------------------------------------- |
| `docker`  | `docker exec $CONTAINER command -v docker`                                                                                                                                                                                        | `INFRA_DOCKER`  | Docker client in container (docker-in-docker) |
| `api_key` | `docker exec $CONTAINER bash -lc 'case "${FORGE_QA_PROVIDER_PROFILE:-openrouter}" in openrouter) test -n "${OPENROUTER_API_KEY:-}" ;; remote-litellm) test -n "${LITELLM_API_KEY:-}" && test -n "${LITELLM_BASE_URL:-}" ;; esac'` | `INFRA_API_KEY` | Selected provider credentials are available   |

Store probe results in the state file:

```bash
CONTAINER_ID=$(docker inspect -f '{{.Id}}' "$CONTAINER")

python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" set INFRA_DOCKER <true|false>
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" set INFRA_API_KEY <true|false>
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" set CONTAINER "$CONTAINER"
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" set CONTAINER_ID "$CONTAINER_ID"
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" set RUN_SCOPE "container:$CONTAINER_ID"
```

`RUN_SCOPE` ties prerequisite satisfaction to the current container instance, so a rebuilt container cannot inherit
side-effect-dependent sections from an old run by accident.

Tell the user which infrastructure is available and what will be skipped.

**Resume** (`--from X.Y`): Read `$STATE_FILE` directly from the host, then validate it against the chosen resume point:

```bash
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" validate "$STATE_FILE" --from <X.Y from --from>
```

This clears stale future-step records and refreshes derived section status for the current run before execution resumes.
The `record` command still validates checklist hash on each call, so hash drift is caught automatically. Show progress:
"Previously: N sections, M passed, K failed. Resuming from X.Y."

On resume, preserve `$STATE_DIR/logs`, `$STATE_DIR/forge-logs-snapshots`, and `/root/.forge/logs` so evidence from the
earlier part of the same QA run remains available.

#### Phase 3: Build Section Index

Run the checklist parser to get the full structure:

```bash
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" index
```

This returns JSON with all sections, subsections, annotations, and assertion counts. Store this as the checklist index.

If category names were given, filter the index to matching sections only.

#### Phase 4: Execute Sections (Main Loop)

For each section in the index (or starting from `--from X.Y`). If `--to X.Y` was set, stop **before** reaching that step
— do not execute it or anything after it. `--to` accepts both section-level (`--to 7` stops before section 7) and
subsection-level (`--to 7.3` stops before step 7.3) IDs. When the stop point is reached, skip to Phase 5 (Summary).

For each section/step in the filtered range:

01. **Read the section file** on the host (path from the index) using the Read tool. Keep reads scoped to a single
    section file (do not load multiple sections at once).

02. **Get step details** for each subsection via the parser:

    ```bash
    python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" step <N.X>
    ```

    This returns JSON with:

    - `annotation` / `annotations`: step type(s)
    - `code_blocks`: list of `{code, runnable}` objects
    - `instructions`: prose for the user
    - `assertions`: list of assertion texts to verify
    - `assertion_count`: number of assertions (deterministic -- do not count manually)
    - `next`: ID of the next step (or null if last)

03. **Annotations** map to step types. Never show raw HTML comments in output.

    | Annotation               | Step type     | Preamble                                                 |
    | ------------------------ | ------------- | -------------------------------------------------------- |
    | `<!-- auto -->`          | `[Automatic]` | "Automatic step -- running checks."                      |
    | `<!-- human:confirm -->` | `[Review]`    | "I'll run this and show you the output for review."      |
    | `<!-- human:guided -->`  | `[Hands-on]`  | "Your turn -- here's what to do in the container shell." |

    **Handle by annotation type**:

    | Annotation                | Action                                                                                                                                                                                                                                                                 |
    | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
    | `<!-- auto -->`           | Run bash block via `docker exec`. Check assertions against output. Show results block.                                                                                                                                                                                 |
    | `<!-- human:confirm -->`  | Run bash block via `docker exec`, show output to user. Use AskUserQuestion: "Does this look correct?" (Pass / Fail / Skip). Show results block.                                                                                                                        |
    | `<!-- human:guided -->`   | Show instructions and bash snippet from the checklist. Do NOT run the bash block. Use AskUserQuestion with context-appropriate framing (see rule 9). After user confirms, verify artifacts via `docker exec` (rule 9). Show results block.                             |
    | `<!-- requires: X -->`    | Split `X` on commas, uppercase each token to form `INFRA_<TOKEN>` (e.g., `docker,api_key` checks `INFRA_DOCKER` and `INFRA_API_KEY`). Look up each via `var get`. Skip if any is unavailable: show `[Skipped -- requires: X]`.                                         |
    | `<!-- prereq: N, ... -->` | Section-level or subsection-level prerequisite. Lists section numbers (e.g., `0, 2, 4`) that must be satisfied in the current run before this section can run. On `--from` resume, check state file for each prereq and warn the user about any blockers. See rule 10. |
    | `<!-- destructive -->`    | Safe inside Docker. Run the bash block, check assertions.                                                                                                                                                                                                              |
    | No annotation             | Treat as `<!-- human:confirm -->`.                                                                                                                                                                                                                                     |

    A subsection can have multiple annotations (e.g., `<!-- destructive -->` + `<!-- human: ... -->`). Apply all that
    match. `requires` is checked first (skip before attempting anything else). `prereq` is checked at section entry.

04. **Execute bash blocks** from the checklist -- run ONLY what the checklist specifies:

    ```bash
    docker exec "$CONTAINER" bash -lc 'cd /workspace && <bash block from checklist>'
    ```

    The agent does NOT invent commands. It runs the checklist's bash blocks verbatim. For each entry in the step's
    `code_blocks` where `runnable` is `true`, run `code` as one Bash tool call. Entries where `runnable` is `false` are
    display-only snippets for `human:guided` steps.

    **Default debug logging**: the QA container exports `FORGE_DEBUG=1` via `/etc/profile.d/forge-qa.sh`, so Forge
    commands write debug logs to `/root/.forge/logs/...` unless the subcommand is explicitly exempt.

    **Before a block that contains `forge logs --clean`**, snapshot the current Forge debug logs into the mounted state
    dir so evidence survives the cleanup step:

    ```bash
    docker exec "$CONTAINER" bash -lc 'SNAP="/workspace/.forge/qa/forge-logs-snapshots/N.X/pre-clean"; rm -rf "$SNAP"; if [ -d /root/.forge/logs ]; then mkdir -p "$SNAP" && cp -R /root/.forge/logs/. "$SNAP"/; fi'
    ```

05. **Check assertions**: For each assertion text from the step details, examine the command output and judge whether it
    is satisfied. This is the adaptability layer -- if CLI output format changes slightly, the agent can still verify
    the intent of the assertion. Classify each assertion as `p` (pass), `f` (fail), or `s` (skip).

06. **Write logs** inside the container -- save raw command output to per-subsection log files:

    ```bash
    docker exec "$CONTAINER" bash -c 'cat > /workspace/.forge/qa/logs/N.X.log <<'"'"'EOF'"'"'
    <raw output>
    EOF'
    ```

07. **Record results** in the state file after classifying each step's assertions:

    ```bash
    python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" record "$STATE_FILE" <N.X> <results>
    ```

    Where `<results>` is comma-separated: `p` (pass), `f` (fail), `s` (skip) -- one per assertion. Example:
    `record "$STATE_FILE" 3.1 p,p,p,p` for a step where all 4 assertions passed. The output shows progress:
    `3.1: 4/4 pass | Section 3: 4/30 | Overall: 75/N`.

08. **Step presentation format**: Every subsection follows a visual pattern so progress is easy to scan.

    ```
    --- N.X Step Title [Type] -------------------------
    <preamble from annotation table above>

    <body: commands, output, or instructions>

    Results:
      ✔ First assertion passed
      ✘ Second assertion FAILED: reason
      o Third assertion skipped
    ----------------------------------------------------
    ```

    **`[Hands-on]` body template** -- guided steps use a fixed inner layout so every run looks the same:

    ```
    --- N.X Step Title [Hands-on] -------------------------
    Your turn -- here's what to do in the container shell.

    In the container shell (`docker exec -it $CONTAINER bash -l`):

    1. First action
    ```

    command-to-run

    ```

    2. Second action
    ```

    another-command

    ```

    Expected:
    - First assertion text from checklist
    - Second assertion text from checklist

    If something goes wrong: <failure cue from checklist, if any>

    Review the instructions above, then answer below.



    <AskUserQuestion>
    ```

    Rules for the template:

    - **"In the container shell:"** (or **"In Session B:"** for live Claude steps) -- always anchor where
    - **Numbered steps** with flush-left code blocks -- no indentation so copy-paste has no leading spaces
    - **"Expected:"** bullet list pulled from the checklist assertions -- tells the user what to look for
    - **Failure cue** line only if the checklist includes one (e.g., "If Claude only says Command completed...")
    - Never rephrase checklist instructions as prose -- copy the structure, fill in runtime values
    - The buffer line and blank lines before AskUserQuestion are mandatory (rule 9)

    **Section boundaries** appear between sections (not between steps within a section):

    ```
    Section N Complete: X/Y passed

    ====================================================

    --- M.1 First Step [Type] -------------------------
    ```

    Use `---` (thin) for step boundaries, `===` (thick) as a single separator line between sections. Use ✔ for pass, ✘
    for fail, o for skip.

09. **For `human:confirm` and `human:guided` items**: CRITICAL -- print the full instructions and bash snippet from the
    checklist **before** calling AskUserQuestion. Do **not** end immediately on the last instruction line or code fence:
    Claude Code's dialog overlays the bottom few terminal lines. After the real instructions, print one short disposable
    buffer line such as `Review the instructions above, then answer below.` and then print **at least three blank
    lines** before calling AskUserQuestion. Treat that buffer line and blank space as sacrificial padding. The user must
    see what to do BEFORE being asked to confirm. The instructions appear in the step body between the opening preamble
    and the AskUserQuestion call. If you put instructions after the question, the user sees only the question with no
    context.

    **Match question framing and options to the step type:**

    | Step asks user to...              | Question style                  | Options                            |
    | --------------------------------- | ------------------------------- | ---------------------------------- |
    | Confirm output looks correct      | "Does this look correct?"       | Pass / Fail / Skip                 |
    | Perform an action (open, launch)  | "Have you [action]?"            | Done / Skip / Stop QA              |
    | Verify something (status, output) | "[Expected result] visible?"    | Yes / No, something's wrong / Skip |
    | Both (run command + check result) | "Did [expected result] appear?" | Yes / No, something's wrong / Skip |

    Keep the AskUserQuestion prompt itself short enough to fit on one line when possible. Put detail in the printed
    instructions, not in the dialog. Don't use "Done" as an answer to a yes/no question. "Did the install succeed?"
    needs Yes/No, not Done.

    The user acts in the container shell. If they choose "Stop QA", skip all remaining sections and go to Phase 5
    (Summary).

    **Do not invent Claude availability failures**: For guided steps that involve a live Claude Code session
    (`forge session start`, `forge session resume`, `forge claude start`, plan mode, Session B, status line checks,
    etc.), do **not** recommend "Skip" merely because the agent cannot drive the TUI itself. Recommend "Skip" only when
    you have concrete evidence that live Claude launching is unavailable in the QA container:

    - A direct probe fails, for example:

      ```bash
      docker exec "$CONTAINER" bash -lc 'command -v claude >/dev/null 2>&1'
      ```

    - The user reports an actual launch failure such as `claude: command not found`.

    If the current run already contains evidence that Claude launched successfully (welcome banner, successful
    `forge session start`, prior guided step, etc.), treat live Claude as available and ask the user to proceed with the
    guided instructions instead of steering them toward `Skip`.

    **Post-confirmation verification**: After the user says "Done", verify that the step actually produced expected
    artifacts before recording results. For each assertion, check whether it can be verified programmatically via
    `docker exec` (file exists, permissions correct, command output matches). Run those checks and record `p`/`f` based
    on the actual result -- not the user's word alone. Only trust the user's confirmation for assertions that are purely
    observational (e.g., "input was hidden", "prompt appeared") where no container state can be checked.

10. **Prerequisite checks** (`<!-- prereq: N, ... -->`):

    Section completion is tracked **automatically** by the `record` command. When the final subsection of a section is
    recorded in the current run scope, `record` sets `SECTION_<N>_STATUS` to `passed` or `failed` in the state file. No
    manual `var set` is needed.

    **When entering a section** (or subsection) with prereqs in its `step` output, run:

    ```bash
    python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" prereq-check "$STATE_FILE" <step_id>
    ```

    This returns `{"ok": true/false, "required": [...], "missing": [...], "blocking": [...], "statuses": {...}}`.

    - If `ok` is `true`: proceed normally.

    - If `ok` is `false`: check the `resolvable` list in the response. `resolvable` contains step-level prereqs (e.g.,
      `4.2`) whose section prereqs are already satisfied -- meaning you can run that step immediately.

      **Auto-resolve resolvable prereqs**: For each step in `resolvable`, fetch its details via
      `walkthrough-state.py step <prereq_id>`. Only auto-run if the step's annotation is `auto` (not `human:guided` or
      `human:confirm`) and it has no unmet `requires:` gates. For interactive prereqs, ask the user instead. Execute
      auto steps normally (run bash blocks, check assertions, record results), then re-run `prereq-check` for the
      original step. This avoids unnecessary skips when the missing prereq is cheap to run.

      **If blocking prereqs remain after auto-resolution** (section-level prereqs, or step-level prereqs whose own
      section prereqs aren't met): warn the user which prerequisites are blocking (show `blocking` and `statuses`).
      `missing` is the subset that was never completed in this run; `failed` and `stale_run` also block. Ask whether to
      (a) run the blocking prereqs first, (b) skip this section/step, or (c) proceed anyway (risky). This handles both
      `--from` resume (skipped sections) and container rebuild (lost state).

    Prereqs are **not transitive** -- only the directly listed sections are checked. Each section already lists its full
    dependency set (e.g., section 5 lists `0, 2, 4`, not just `4`).

11. **Gate rules** -- check after each section completes:

    | If section fails... | Then...                                                              |
    | ------------------- | -------------------------------------------------------------------- |
    | 0 (Enable)          | Stop. Enable is broken.                                              |
    | 2 (Extensions)      | Skip Section 3 (can't verify auth without ext).                      |
    | 4 (Proxy)           | Skip Sections 7, 14-16 (no proxy for costs/workflow/skills/handoff). |
    | Any section         | Section 20 (Cleanup) always runs.                                    |

12. **Context conservation**: After completing each `## N.` section, print a one-line summary using the progress numbers
    from the last `record` output. Do NOT carry raw command output forward -- the state file and logs inside the
    container have the details. This preserves context window for the full run.

**Glue calls need no narration.** The `walkthrough-state.py step`, `record`, and `var` calls between steps are
bookkeeping. The Bash tool will show their JSON output in the transcript -- that's fine. But do NOT add commentary
around them ("now let me fetch the next step", "the JSON shows..."). Just call the tool and proceed to the next visible
step. The user should see a clean flow of steps, not a play-by-play of the bookkeeping layer.

**Variable substitution**: When commands in bash blocks use placeholders like `<proxy_id>`, capture runtime values and
store them in the state file:

```bash
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" set PROXY_ID <value>
```

Retrieve when needed for substitution in later steps:

```bash
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" var "$STATE_FILE" get PROXY_ID
```

#### Phase 5: Summary

Get the final report from the state file:

```bash
python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" report "$STATE_FILE"
```

This returns JSON with per-section pass/fail/skip counts, failures list, gaps, and totals. The script provides all
numbers -- do not count manually. Render the report JSON as a results table:

```
Full QA Results
====================================
  Container:       $CONTAINER
  Checklist:       v1.0.0 (N items)

  Section                  Pass  Fail  Skip
  ----------------------------------------
  0. Install               17     0     0
  1. Pre-Flight              2     0     0
  2. Extensions             26     0     0
  ...
  ----------------------------------------
  TOTAL                   290    3    22

  Failures:
    2.3  Verify Pre-Existing Settings: ...
    6.4  Smoke Test SessionStart Hook: ...

  Skipped (infra missing):
    3.1-3.11 (requires: api_key)
====================================
```

#### Phase 5b: Save Run Artifacts

After generating the report, save all artifacts to a timestamped run directory.

This phase is required for every QA run, including partial `--from/--to` runs and runs with failures. Do not stop after
printing the summary. A QA run is not complete until `report.md`, `state.json`, and `.pending-transcript` exist in the
run directory / state dir.

After Phase 5 summary, continue directly into Phase 5b without asking the user whether to save artifacts.

```bash
RUN_DIR="$STATE_DIR/runs/$(date +%Y-%m-%d-%H%M%S)"
mkdir -p "$RUN_DIR"
```

1. Generate the report using `walkthrough-state.py`:

   ```bash
   python3 "$SCRIPTS/walkthrough-state.py" "$CHECKLIST" report "$STATE_FILE"
   ```

   This returns JSON with per-section pass/fail/skip counts, failures, and gaps. Find the report template
   (`${CLAUDE_SKILL_DIR}/resources/report-template.md`), fill it in, and write to `$RUN_DIR/report.md`.

2. Copy the state file: `cp "$STATE_FILE" "$RUN_DIR/state.json"`

3. Copy mounted raw step logs when present:

   ```bash
   if [ -d "$STATE_DIR/logs" ]; then
     cp -R "$STATE_DIR/logs" "$RUN_DIR/step-logs"
   fi
   ```

4. Copy any pre-clean Forge log snapshots when present:

   ```bash
   if [ -d "$STATE_DIR/forge-logs-snapshots" ]; then
     cp -R "$STATE_DIR/forge-logs-snapshots" "$RUN_DIR/forge-logs-snapshots"
   fi
   ```

5. Copy the container's current Forge debug logs when present:

   ```bash
   if docker exec "$CONTAINER" bash -lc 'test -d /root/.forge/logs'; then
     mkdir -p "$RUN_DIR/forge-logs/final"
     docker cp "$CONTAINER:/root/.forge/logs/." "$RUN_DIR/forge-logs/final"
   fi
   ```

6. Generate a transcript claim token and write the marker so only this QA session can copy the transcript here when it
   ends:

```bash
TRANSCRIPT_TOKEN="forge-qa-transcript-token:$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
python3 - <<'PY' "$RUN_DIR" "$STATE_DIR/.pending-transcript" "$TRANSCRIPT_TOKEN"
import json
import sys

run_dir, marker_path, token = sys.argv[1:4]
with open(marker_path, "w", encoding="utf-8") as handle:
    json.dump({"run_dir": run_dir, "transcript_contains": token}, handle)
    handle.write("\n")
PY
```

Tell the user: "Run artifacts saved to `$RUN_DIR`. Forge step logs and debug logs were copied when present. Transcript
claim token: `$TRANSCRIPT_TOKEN`. Transcript will be added when this QA session ends."

#### Phase 6: Cleanup

- If all passed and `--keep` was NOT set: stop and remove the container.
- If any failures: keep the container for inspection. Print: "Container kept for inspection. Run `/forge:qa --stop` to
  remove."
- The last `record` call already updated `last_updated` in the state file.

Tip: "Report and transcript saved to the run directory. Find previous reports in `~/.forge/manual-testing/qa/runs/`."

## Safety Model

| Tier    | Scripts involved              | What can go wrong      | Mitigation                             |
| ------- | ----------------------------- | ---------------------- | -------------------------------------- |
| Full QA | `start-container.sh` + Docker | Nothing -- OS boundary | Container cannot reach host filesystem |

All commands run inside the Docker container via `docker exec`. The container is the sandbox.

`walkthrough-state.py` runs on the HOST for bookkeeping (state file is accessible via mount). It never executes commands
inside the container.

## Reference: Full QA Checklist

The full checklist is split:

- Index: `resources/checklist.md`
- Sections: `resources/checklist/*.md`

It covers 21 categories:

| Category    | Section | Destructive? |
| ----------- | ------- | ------------ |
| enable      | 0       | Yes          |
| preflight   | 1       | No           |
| extensions  | 2       | No           |
| auth        | 3       | No           |
| proxy       | 4       | No           |
| session     | 5       | No           |
| hooks       | 6       | No           |
| costs       | 7       | No           |
| status-line | 8       | No           |
| commands    | 9       | No           |
| resume      | 10      | No           |
| config      | 11      | No           |
| search      | 12      | No           |
| guard       | 13      | No           |
| workflow    | 14      | No           |
| skills      | 15      | No           |
| handoff     | 16      | No           |
| info        | 17      | No           |
| disable     | 18      | Yes          |
| uninstall   | 19      | Yes          |
| cleanup     | 20      | Yes          |

Commands are deterministic (from checklist); interpretation is adaptive (agent judges output).

## Common Mistakes (DON'T)

- **DON'T invent CLI commands.** Run ONLY commands from the checklist's bash blocks. If a command doesn't exist, the QA
  run will show a confusing error.
- **DON'T carry raw output forward.** After each section, summarize and drop. The state file and logs inside the
  container have the details. This preserves context window for the full run.
- **DON'T count assertions manually.** Use `walkthrough-state.py record` and `report` for all counting. LLMs get
  arithmetic wrong.
- **DON'T combine multiple Bash commands in one call.** Run each `code_blocks` entry as a separate Bash call. Piped
  multi-command blocks fail silently in the Bash tool.
- **DON'T put instructions after AskUserQuestion.** The user sees the question modal immediately -- anything you print
  after it appears below their answer, not above the question. Print instructions BEFORE the tool call.
- **DO add a real visual buffer before AskUserQuestion.** Use a short sacrificial buffer line plus at least three blank
  lines so the dialog covers padding, not the instructions or command snippet.
- **DON'T ignore script failures.** If `start-container.sh`, `docker exec`, or `walkthrough-state.py` exits with a
  non-zero code, STOP. The error message tells you what went wrong (count mismatch, hash drift, corrupt state). Do not
  proceed with stale data.
- **DON'T assume Claude Code is unavailable without evidence.** For `human:guided` live-session steps, only recommend
  `Skip` after a real failed probe (`command -v claude`) or an actual user-reported launch error.

## Tips

- **Context window**: Full QA may be long-running -- use `--from X.Y` to resume after compaction.
- **Run a range**: Use `--from 4.1 --to 7` to run sections 4 through 6 only (excludes the `--to` step).
- **Resume after compaction**: If the conversation compacts during QA, use `/forge:qa --from X.Y`.
- **Quick check**: For a quick non-interactive health check, use `/forge:smoke-test`.
