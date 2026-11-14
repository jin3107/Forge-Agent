# Workspace hook trust: implementation plan

Current status: **partial enforcement only**. Normal non-demo, non-synthetic server chat and the manual compact endpoint use the scoped checked hook runner and process-local admin decisions; foreground and background subagents created during such a chat receive the runner. Demo, synthetic operations, scheduled turns, batch/battle, direct library callers and other entry points remain outside this gate and may execute unapproved hooks. `enforcement_active: false` means full coverage is not active; `enforcement_scope: normal_non_demo_chat_and_manual_compact` describes the partial path. Decisions expire, are lost on restart and are not shared across workers. Checks race and do not make Forge an OS sandbox. The sections below are an incremental history: older claims of no gate or inert decisions describe the stage when they were written, not the current partial status.

## Observed behavior

`engine/hooks.py` reads lifecycle hook definitions from `<workspace>/.forge/config.json`, falling back to global configuration when the workspace `hooks` value is falsey. A local temporary-workspace experiment with an empty global configuration confirmed that `run_event(..., 'user_prompt_submit', ...)` executes the workspace command without a separate trust check. `Agent.send` invokes this event before chat mode validation. This was a direct runner experiment, not an HTTP/browser exploit demonstration.

A nonempty workspace `hooks` mapping overrides the entire global lifecycle-hook mapping even if every workspace event list is empty or the only event is unknown; this can suppress a global pre-tool hook. The independent inspector now marks such mappings as requiring review, but the production runner remains unchanged. A nonempty legacy workspace `on_file_change` also wins selection inside `_hook_cfgs`; that entry is skipped by `post_tool_use`, whereas `tools.execute_tool` uses the global legacy value separately. A review must describe these effects, not just list commands.

The implementation also has separate paths: `session_start_cache`, general lifecycle events, pre/post-tool hooks, fan-out subagents, notification, stop, `compact_session`, and global legacy `on_file_change` in `tools.execute_tool`. Multiple agent callers currently catch and ignore hook exceptions; a trust exception cannot simply be added without changing those boundaries. Demo mode, batch/battle/scheduled turns, subagents and worktree workspace selection need explicit review. Workspace `on_file_change` is parsed by `_hook_cfgs` but its legacy entry is skipped by `post_tool_use`; `tools.execute_tool` separately reads the global value. Do not assume these two paths are equivalent.

## Required properties

1. Unapproved workspace lifecycle hook configuration must not execute, and the affected operation must not silently proceed as though its pre-hooks ran. Give the user a clear blocked result before consequential work. Preserve user input and explain what to review.
2. Display the actual workspace identity and the proposed hook configuration/commands before approval. Treat workspace-provided descriptions and commands as untrusted text, not HTML. Highlight that commands run on the host under the server account, and may read/write files or access the network.
3. Approval must be an authenticated, CSRF-protected, explicit admin action, never model/tool-controlled. The user selected **admin only**. Check the owner/session and relevant workspace on both review and approval. Existing session ACLs make another user's session return 404 even to an admin, so approving a member's workspace requires a deliberate, scoped delegation/request workflow; do not bypass ACL by giving administrators blanket access to session content. That workflow and its consent, revocation, role-change and stale-request semantics are not yet implemented.
4. Bind approval to the reviewed configuration bytes and an unambiguous workspace identity; require an expected digest on approval and compare with freshly read bytes. Missing, malformed, oversized, unreadable, replaced or changed configuration must fail safely. Invalid/missing approval must not cause global fallback to masquerade as workspace-hook enforcement.
5. Recheck trust at every execution boundary, not only at chat start; prevent exception handlers from swallowing a trust denial and then performing the protected action. Config may change mid-turn. A standalone prototype now releases immutable checked bytes and selects entries without reopening the file; it is not connected to the runner, and a previously returned snapshot is not automatically invalidated by later changes or revocation. Specify meaningful behavior for post-tool, stop and notification events when the preceding operation has already occurred: report incomplete enforcement; never claim rollback.
6. Handle session-start cache explicitly: production cache now separates resolved workspace and selected session-start definitions, but this is not a trust check and changing a workspace or selected hook may run that hook again without approval. Changes to referenced scripts do not invalidate it, concurrent misses can execute more than once, and old in-flight callers retain their own result. Before connecting enforcement, ensure approval is checked even on a cache hit and define revocation, workspace switch, session deletion, restart, and multi-worker behavior.
7. Keep global operator-configured hooks separate from workspace hooks. Document precedence and compatibility; do not accidentally promote workspace configuration into global trust. Verify global `on_file_change` separately.
8. Approval must be revocable. The standalone prototype uses bounded process-local in-memory records, so approvals are lost on restart and are not shared across workers; it has not been selected as a production deployment guarantee or integrated. Define persistence, atomicity and concurrent updates, migration, session cleanup and multi-worker consistency before claiming those guarantees. If persistence is offered and cannot be confirmed, deny approval rather than report success.

## Security limitations to preserve in the UI and documentation

A hash of `.forge/config.json` only binds those bytes. A command can call a mutable script, executable, dependency or shell expansion; those may change after approval without changing the configuration. Approval therefore trusts execution in that workspace, not just inert configuration text. No practical complete dependency closure is established here. Filesystem/path races, mutable referenced files, external writers, OS-user privileges, secrets, network access, process trees and Docker bypass remain. A pre-execution recheck narrows but does not eliminate TOCTOU. Global hooks may also execute workspace-controlled files. Forge is for trusted users and is not an OS isolation boundary.

## Implementation sequence and acceptance checks

1. Bounded workspace configuration inspection is implemented independently in `engine/hook_inspection.py` and covered by nine focused tests. It is not connected to any product flow. Effective source precedence, global hooks, strict-grammar compatibility and preview presentation still require integration and testing; ensure no file write or shell execution occurs during preview.
2. Implement the smallest fail-closed trust check at the actual workspace-hook execution boundary; explicitly propagate structured denial through all callers. Ensure an untrusted pre-hook cannot be bypassed by broad `except Exception`. Test real shell marker commands, changed config between checks, all events, all chat modes, compaction, subagents and background/notification/stop paths. Define outcomes where the action already happened.
3. Add the review/approval/revoke contract, authenticated scope and concurrent update handling. Test anonymous, CSRF failure, cross-account/session access, mismatched workspace or digest, missing/invalid config, config replacement and revoked/changed approval. Exercise true HTTP with a deterministic fake provider. Do not claim script integrity from a config digest.
4. Add frontend review and approval flow with clear command display, preserve unsent prompts and drafts, handle stale or failed approvals without automatically retrying destructive work, and verify React and real Chromium scenarios. Explicitly test malicious markup displayed as text.
5. Verify CLI and non-browser paths, scheduler/batch/demo, documentation, focused repeated tests, full Python and applicable frontend/browser suites. Update the English implementation report with exact results, remaining races and compatibility changes. No production rollout or commit without an explicit request.

The stages are a plan, not a claim that an incomplete intermediate stage is safe to deploy. Do not ship the trust gate until there is a usable approval/recovery path and the integration paths above are validated.

## Current review-request invalidation (foundation only)

Admin detail and qualifying decision validation remove the original process-local request and its inert decision when they observe unavailable scope or stale/invalid configuration. Restoration requires a new request. Conditional removal preserves a replacement but removes a decision made concurrently on the same stale request. Lists do not revalidate, some decision conflicts return before validation, and unobserved ABA, races after checks, mutable referenced scripts, restart and multi-worker inconsistencies remain. This is not an execution gate and does not block untrusted hooks. See the implementation progress report for tests and current scope.

## Snapshot validation consistency (foundation only)

The prototype selector applies the same bounded full grammar and classification as file inspection to supplied snapshot bytes; matching hashes alone are insufficient. This catches inconsistent labels and malformed unselected events, but does not authenticate a fabricated approval or prove an absent snapshot corresponds to disk. It is not connected to hook execution; all prior gate, race and persistence requirements remain.

## Admin detail final request check

Admin detail rechecks the original request after its final scope validation; a revoke, expiry, replacement or decision observed during that validation prevents the old response. This does not make response delivery atomic with revocation or filesystem/account changes, nor retract previously sent content. This remains a process-local review feature, not a hook execution gate.

## Compaction trust-denial propagation (not enforcement)

A reserved `HookTrustDenied` exception now escapes the broad pre-compact error handler. An injected denial stops automatic compaction and its agent turn; the HTTP compact handler returns a structured failure, while chat SSE reports an error and skips post-turn auto-commit. This does not cause the current hook runner to raise the signal and does not cover other hook events, all callers or server preprocessing. Integration and end-to-end authorization remain required.

## Turn-start trust-denial propagation (not enforcement)

`Agent.send` now propagates an injected `HookTrustDenied` at `user_prompt_submit` and `session_start_cache`; the chat SSE handler reports it and skips post-turn auto-commit. The existing implementation does not generate this signal or check trust on cache hits. Reruns/background children skip prompt-submit, quick-memory and server synthetic paths can bypass session-start, and compaction precedes session-start. Other caller wrappers may catch the exception, so this is not an end-to-end gate.

## Pre-tool trust-denial propagation (not enforcement)

Injected trust denials at ordinary pre-tool checks or fan-out pre-dispatch checks escape the agent loop; unfinished tool calls in its current stored assistant message are closed with a cautious interruption notice. Earlier completed work is not rolled back. The chat error warns to inspect workspace state. The production hook runner does not generate the denial, and running subagents, post-tool, stop, notification, other entry points, persistence failure and multi-worker execution remain unresolved.

## Post-tool trust-denial handling (not enforcement)

An injected denial after ordinary tool dispatch preserves the returned status/output and applicable event metadata, warns that effects were not rolled back, closes unresolved transcript entries, saves before yielding the tool result, and then aborts the turn. Storage or response delivery can fail; already dispatched work cannot be undone by this mechanism. The production hook selector does not raise the denial and other hook/caller paths remain unresolved.

## Serial-subagent denial propagation (not enforcement)

An injected `HookTrustDenied` from a serial child now escapes its wrapper rather than becoming an apparently successful delegation result. Prior child activity is not rolled back. At that point parallel fan-out still caught child exceptions; see the later fan-out section. Background tasks have separate error reporting. The production hook selector still does not produce trust denials.

## Fan-out denial propagation (not enforcement)

Injected child denials now escape fan-out pumps. On detection the current chunk requests cooperative cancellation of sibling pumps and awaits them before propagating; later chunks are not started. Cancellation or generator close also attempts pump cleanup. Uncooperative tasks may block the join, detached processes and completed side effects may survive, and no rollback is implied. Each pump now receives its own isolation argument; this is not proof that a worktree is an isolation boundary. The hook runner still does not produce the signal.

## Stop-hook denial propagation (not enforcement)

Normal `Agent.send` now runs stop before emitting `turn_end`; an injected trust denial propagates to chat SSE, which reports it and skips post-turn auto-commit. This changes event ordering and can delay turn completion. Earlier outputs and side effects are not undone, background children and some early returns skip stop, and the production hook runner does not yet raise this denial.

## Notification trust-denial propagation (not enforcement)

Injected notification trust denials now escape `_notify`; command and MCP approval flows notify before registering the request, avoiding an unpresented request specifically when that notification denies. Background completion catches the denial separately, marks its process-local notification status and attempts to notify the lead without claiming rollback or delivery. Other cancellation windows and background policy remain unresolved. The hook runner still does not produce trust denials.

## Background child denial reporting (not enforcement)

Injected child trust denials now record an error and distinct process-local code, with a warning about possible earlier effects, rather than an undifferentiated background error. Parent inbox delivery is best effort; failures are recorded in memory, and the status tool displays the available codes. This does not stop an independently running parent, undo prior work, provide durable status or activate the hook trust gate.

## Scheduled-loop denial propagation (not enforcement)

A scheduled run that raises an injected `HookTrustDenied` now stops its process-local loop, records a structured reason, and reports it through status and `/loop`. It does not retry automatically or undo previous work. Restart/removal loses status, a user can start a new loop, and demo-mode or error events that do not raise are not covered. The hook runner still does not generate the denial.

## Batch/battle denial propagation (not enforcement)

Injected child trust denials now produce per-task structured errors, request cancellation of running siblings and produce non-success terminal status; UI task errors persist through terminal events. Cleanup on batch/battle stream closure is attempted. This is cooperative only, can hang on uncooperative children, cannot undo prior effects or guarantee external process termination, and is not proof of delivery. The hook runner does not yet generate trust denials.

## Approved review snapshot resolver (not enforcement)

An internal unused server prototype resolves an explicitly shared, approved request to fresh inspected bytes after scope and deciding-admin revalidation, with registry expiry/replacement checks under a process-local lock. A caller must authenticate the intended execution scope; returned snapshots can become stale immediately. It handles approved hook-bearing configurations only and is not connected to execution. Filesystem/authorization races, ABA, mutable scripts, lock-held I/O, multi-worker and durable/revocation semantics remain unresolved.

## Checked general-event runner experiment (not production enforcement)

A separate, unused runner executes four general lifecycle events from a resolver-approved hook-bearing snapshot and re-resolves before later commands; marker tests exercise approved, pending, changed and revoked cases. It trusts the resolver and caller, at that stage did not implement absent/no-hooks or tool/cache paths, and cannot prevent changes immediately after a check or stop running commands. It is not connected to the existing hook runner and cannot be represented as a production gate.

## Absent and hook-free scope snapshots (not enforcement)

An unused scope resolver distinguishes observed absent and validated hook-free configurations from hook-bearing ones, invalidates an old request on an observed scope/config transition, and checks admin approval for hook-bearing bytes. The standalone general-event runner can use global lifecycle hooks for the absent/no-hook cases. Trusted callbacks and scope are not an authentication boundary; returned snapshots can go stale and global hooks may run workspace-controlled content. No product execution path calls this prototype.

## Checked pre/post-tool experiment (not enforcement)

Unused checked pre/post-tool helpers now select from a resolver snapshot before matcher filtering and recheck before later matching hook commands. Tests use POSIX markers for pending, approved and revoked cases and preserve the legacy post-tool skip. They do not guard actual `Agent.send` or `tools.execute_tool`, the separate global legacy hook path, or session-start cache. Prior actions are not rolled back; trusted resolver inputs and all TOCTOU/multi-worker limitations remain.

## Checked session-start cache experiment (not enforcement)

An unused bounded single-event-loop cache prototype revalidates on hits, uses the review-request identity as a cache discriminator for approved hook-bearing configurations, and rechecks after execution. Revoking and creating a new approval for identical bytes does not reuse that prior cached output. Identity is not a credential, concurrent misses may both run, old callers can receive results and side effects cannot be rolled back. The prototype is not connected to production hooks; ABA, filesystem races, multi-worker state, mutable scripts and session cleanup remain unresolved.

## Authenticated HTTP experiment with test-only adapter

A real Uvicorn test fixture explicitly monkeypatches the incomplete checked-hook prototypes into selected `Agent.send` paths to exercise member/admin review, caching, changed config and revocation while a hook runs. This is not product wiring and must not be interpreted as active enforcement. It uses a fake provider and POSIX marker hooks; all other deployment, race, event and caller limitations remain.

## Legacy `on_file_change` boundary characterization

Real `execute_tool` write/edit invokes only global `config.load_config().get("on_file_change")` after the write and separately from lifecycle post hooks. Checked post skips legacy entries; the workspace legacy command may appear in selection/review but is not executed by this tool path. Six POSIX characterization tests demonstrate direct execution without checked pre, and a config change between checked pre and the real write/global legacy hook before checked post denies. This is not evidence that workspace users can edit global config. Any future integration needs an explicit global-hook policy, actual execution-boundary coverage and compatibility decisions; pre/post checks alone do not make file effects transactional. No production gate was introduced.

## Checked global legacy experiment (not integration)

The standalone `checked_global_file_change` prototype validates a supplied workspace scope and global command before and after execution. It preserves the documented global-only selection rather than silently activating workspace legacy commands. It requires a trusted caller/resolver and a current server-wide config; rereading the same supplied dictionary does not detect external config changes. Its strict command typing and sanitized launch errors differ from the real tool. Existing file writes and hook effects are irreversible, and cancellation/timeout cannot establish process-tree cleanup. It is unused by the product and is not an enforcement gate.

## Legacy prototype cleanup experiment (not production)

The unused global legacy adapter now attempts POSIX process-group and direct-child termination on timeout/cancellation, with a bounded direct-child wait and unconfirmed cleanup reporting on timeout. A Linux test demonstrates a detached descendant still survives; signal failure, spawn-time/repeated cancellation, PGID reuse, buffering, non-POSIX behavior and production integration remain open. No guarantee or product hook gate is implied.

## Bounded legacy prototype output (not production)

The standalone global legacy adapter drains merged output and keeps at most 64 KiB of output bytes, with a 600-character displayed prefix and explicit truncation indication. A reader failure attempts best-effort cleanup and reports execution/cleanup unconfirmed without exception details. This is not a total resource bound, may discard partial output on timeout, and differs from production for oversized output. At that stage both production captures remained unbounded; later production updates bound retained output for `_run_cmd` and the separate tool legacy hook. No product trust gate exists.

## Authenticated HTTP mid-tool config change characterization

In an opt-in checked-adapter fixture with real auth/session and real `write_file`, the tool itself changes hook-free config to an unapproved hook config. The unchanged production tool runs a fixture-configured global legacy hook after the write and before checked post-hook denial. The tool result and no-rollback warning are sent and persisted. This confirms the earlier direct-call observation through an authenticated Agent flow with a fake provider; it does not enable enforcement, prove a global-config privilege escalation, or resolve any races.

## Production lifecycle-hook capture improvement (not a trust gate)

`engine.hooks._run_cmd` now drains stdout/stderr concurrently while retaining bounded raw prefixes and indicates truncation within the existing output character limits. This production change does not verify hook approval, bound subprocess resources or solve cancellation and process-tree cleanup. A later change also bounds retained output for the separate legacy `tools.execute_tool` hook.

## Production legacy output capture (not a trust gate)

The legacy global file-change hook in `tools.execute_tool` now drains merged output with bounded raw retention and indicates display truncation. Default timeout and execution ordering remain; timeout/cancellation/reader failure do not guarantee process-tree cleanup or roll back file writes. This does not alter approval policy or provide a hook trust gate.

## Demo and HTTP quick-memory boundary characterization

A real authenticated HTTP fixture with pending review and test-only lifecycle adapters shows that `send_demo` still writes its demo file and runs a fixture-configured global legacy hook without invoking checked lifecycle hooks. A `#` quick-memory request also writes despite pending review, but the HTTP endpoint intercepts it as a synthetic response before either agent or demo path. No workspace marker executed in this experiment. These paths need explicit policy and boundary coverage; adding legacy hook calls before a real trust gate would risk executing unapproved commands. Product enforcement remains inactive.

## Ownerless local CLI boundary

An isolated real CLI test shows an ownerless local session runs a workspace prompt hook without server review; the hook's exit 2 short-circuits before provider access. CLI/TUI use unscoped `SessionStore` while server review requires an enabled account and owned session; server scoped stores do not expose an ownerless local session to an arbitrary account. Local enforcement and authenticated server binding/migration need an explicit product policy, not inferred OS authority. This observation does not imply that unrelated non-hook operations must be blocked. Production enforcement remains inactive.

## Local launcher lifecycle selector policy (partial production enforcement)

The user chose to block local workspace hooks until server-admin approval can be authenticated. CLI/TUI launchers now activate a process-local selector that denies hook-bearing or invalid workspace configs and selects global hooks only from a strict absent/hook-free inspection. CLI reports a distinct denial, explaining that local/server session linking is not available yet. This is a partial local-only selector policy, not a server gate: global legacy and demo paths differ, direct library callers may not enable it, existing side effects/races remain, and there is currently no way to use server approval from local sessions. Server `enforcement_active` remains false.

## Local denial UI coverage

A headless Textual test submits a local prompt and `/compact` with the local selector policy active; the TUI now displays `HookTrustDenied` for compact instead of letting it escape the event handler. Interactive CLI `/compact` also reports denial and returns to its prompt. A real CLI subprocess test verifies JSON and terminal errors and global-hook fallback with no workspace config. This does not connect local sessions to server approvals or enforce hooks on the server.

## Lifecycle-hook subprocess cleanup (not a trust gate)

Production `_run_cmd` now uses a new POSIX session and best-effort group/direct-child termination on timeout, cancellation and post-spawn communication failure, with a bounded direct-child wait and explicit timeout uncertainty. Escaped descendants demonstrably survive; already-exited leaders, spawn-time/repeated cancellation, signal failures, process ID races and earlier side effects remain unresolved. At that stage legacy `tools.execute_tool` cleanup was separate; the subsequent legacy cleanup change now also attempts bounded best-effort cleanup. Server hook approval enforcement remains inactive.

## Production legacy subprocess cleanup (not a trust gate)

The global `on_file_change` in `tools.execute_tool` now attempts POSIX group/direct-child cleanup with bounded direct-child waiting after timeout, cancellation or post-spawn reader failure. Timeout explicitly reports cleanup unconfirmed; the earlier file write is not undone. Detached children can still survive, and no server hook-approval gate or sandbox is implied.

## Checked legacy prototype pipe-warning investigation

The unused checked-global-legacy adapter now attempts bounded discard-draining after killing a flooding subprocess, avoiding an observed paused-pipe `ResourceWarning` in the controlled POSIX regression. A warning-free run does not prove cleanup if descendants retain pipes or cancellation interrupts the wait. This prototype change does not establish a server trust gate.

## Session-scoped subagent approval blocker

An authenticated HTTP experiment with a test-only adapter and fake provider confirms that admin approval for a parent session does not authorize a newly created child sharing the same workspace. Approving that child still does not authorize a later newly generated child. This is expected under the current session-scoped record, not active enforcement. Automatic inheritance would broaden an admin decision and needs an explicit product choice; distinct worktree/workspace and modified config must not be silently inherited.

## Subagent isolation dispatch and unresolved workspace routing

Serial `spawn_subagent` now forwards its requested isolation argument. Mocked-worktree/session-store tests show the successful pointer route, but also show `make` returning `None` reports shared workspace twice while the child actually uses its new session workspace. Pointer-write failures are swallowed. Do not claim isolation until failure behavior and actual routing are repaired and tested. No inheritance policy decision was inferred from a generic request to continue; server hook enforcement remains inactive.

## Fail-closed worktree binding and remaining checkpoint-key mismatch

Worktree subagents now fail explicitly without starting when creation, path validation or pointer binding fails; serial and fan-out tool results preserve the failure. A real Git test confirms a session-keyed checkpoint can create a worktree, while a normal workspace-keyed tool checkpoint alone cannot be found by session-keyed `worktree.make`. This mismatch, shared-path failure handling, residual artifacts and filesystem races remain open. The change does not grant subagent hook approval or activate server enforcement.

## Worktree Git index prerequisite

Real Git tests found that worktree diff/merge staged into the common checkpoint repo index. They now operate through the worktree's own Git directory after best-effort common-repo/branch checks, with inherited Git index/path overrides removed. This does not solve the checkpoint-key mismatch, metadata races, externally configured Git behavior, ambiguous empty diff/error response, merge atomicity, cleanup or OS isolation. Server hook enforcement remains inactive.

## Background worktree fail-closed prerequisite

Background worktree requests now use the foreground binding checks and fail without scheduling if the requested worktree cannot be established; the background child revalidates the checked path immediately before it sends. At that stage shared background binding was unresolved; a later shared-binding change now checks it. The checkpoint key mismatch, filesystem races, detached tasks and approval inheritance remain unresolved. This does not activate server hook enforcement.

## Shared subagent binding prerequisite

Foreground and background shared children now bind to and verify the parent's resolved workspace before starting; background checks again when its child starts, and invalid isolation is rejected. Pointer/filesystem races, prior or detached side effects, checkpoint key mismatch and local/server approval scope remain open. These checks are not a sandbox or a server hook gate.

## Discard safety prerequisite before checkpoint-key alignment

Discard now checks the worktree's Git repository and branch, reports failed or partial operations, and does not delete a session-keyed child checkpoint repository. This is best effort, not an atomic filesystem guarantee, and leaves orphan cleanup to investigation. The session-versus-workspace key mismatch and existing worktree compatibility remain unresolved; server hook enforcement remains inactive.

## Checkpoint alignment characterization

A real tool write records a pre-write workspace-keyed undo snapshot, whereas worktree creation expects a session-keyed repo. Directly substituting the workspace key would therefore fork an older tree in the tested case. Details, existing-fork behavior and migration requirements are recorded in `CHECKPOINT_WORKTREE_ALIGNMENT.md`. No migration or server trust enforcement has been implemented.

## Pre-merge undo checkpoint prerequisite

All callers of `worktree.merge` now attempt a workspace-keyed pre-apply checkpoint and receive a warning if it was unavailable; chat no longer snapshots under the session key after merge. Creation and migration keys are still unaligned. Snapshot/apply/undo are not atomic, a failed apply can still add an undo entry, and no server hook gate or approval inheritance is enabled by this change.

## Standalone base capture experiment

A separate child-keyed current-workspace Git capture prototype is present but not wired to subagent execution. It does not authorize parent/child relations, provide atomic snapshots, manage artifacts, align production worktree keys, or activate server hook enforcement.

## Standalone binding consistency experiment

An unused validator checks caller-trusted parent/child/workspace/Git metadata against live scoped sessions. It does not authenticate or store its input: a forged same-owner, same-workspace parent can pass, as a test demonstrates. It provides no production authorization, approval inheritance or server hook enforcement.

## Standalone binding registry experiment

An unused SQLite prototype stores immutable-by-API bindings and supports scoped lookup. A test confirms that it accepts forged input from a caller: it does not authenticate issuers, DB writers or callers. It provides neither production parent/child authorization nor approval inheritance, and server hook enforcement remains inactive.

## Standalone issuance experiment

An unused scoped issuance helper creates a fresh child and records the parent relationship rather than accepting a caller-supplied binding. It relies on the caller to authenticate the scoped store and protect registry writers/files; publication is not atomic with Git or session setup. It does not enable production worktree routing, approval inheritance or server hook enforcement.

## Exact session ID boundary prerequisite

A controlled authenticated HTTP regression demonstrated that stripping invalid characters for session lookup while using the raw ID as a workspace path allowed an owned-session alias to read and write a sibling directory. SessionStore now rejects noncanonical IDs and mismatched embedded session IDs instead of normalizing them. This is not OS isolation, does not validate every separate helper, and does not activate server hook enforcement.

## Direct worktree ID checks

Worktree path constructors and public operations now reject malformed identifiers even for direct callers. Valid identifiers do not prove ownership or a parent-child relationship. This does not activate server hook enforcement.

## Worktree collision preservation

Worktree creation now refuses observed existing targets instead of forcibly removing them, preserving a dirty worktree in the tested retry case. Checks remain raceable and do not provide a server hook gate or sandbox.

## Direct checkpoint key checks

Checkpoint helpers now reject malformed repository keys before path construction or Git; valid keys do not establish authorization. Production worktree key alignment and server hook enforcement remain incomplete.

## Worktree diff path binding

Diff now checks the requested path against the expected child worktree before Git; merge rejects a symlink at that path. A copied `.git` pointer in a different directory was sufficient to pass the prior repo/branch check in real Git. Path races and authorization remain unresolved; server hook enforcement remains inactive.

## Textual worktree Git subprocesses

The textual worktree Git runner now uses bounded retained output and best-effort direct-child timeout/cancellation cleanup. At that stage binary diff/apply and archive/export remained separate and unbounded; binary diff, apply and async archive have since been bounded but sync archive was subsequently bounded; subprocess descendants and partial Git effects are not contained. This does not activate server hook enforcement.

## Bounded binary worktree diff

Merge's binary diff now preserves raw bytes, rejects stdout/stderr overflow and attempts direct-child cleanup on timeout/cancellation. Apply was subsequently bounded; sync archive/export was subsequently bounded, and no server hook gate is enabled.

## Bounded binary worktree apply

Apply now has concurrent bounded output capture, chunked input, timeout and best-effort direct-child cleanup. Failures explicitly warn that workspace changes may already have occurred. At that stage archive/export remained unbounded; async and sync archive have since been bounded. This does not activate server hook enforcement.

## Bounded async worktree archive

Async archive capture is capped at 16 MiB before extraction; the synchronous fork archive has since been bounded; extraction is not transactional and fork may copy current workspace contents when no session-keyed repo exists. This does not activate server hook enforcement.

## Bounded synchronous fork archive

Sync archive capture now has retained-output caps and best-effort direct-child timeout cleanup. At that stage `SessionStore.fork` silently fell back on archive failure/overflow; it now fails when an existing session-keyed repo cannot be exported, while no-session-repo fallback still copies current workspace; extraction and copying are not atomic. No server hook gate is enabled.

## Fork failure behavior

Fork fails instead of switching to live copy when an existing session-keyed snapshot cannot be exported. No-session-keyed-repo fallback still copies the live workspace; partial artifacts may remain. This is not a server hook gate.

## Staged worktree archive extraction

Worktree archive extraction now stages before publishing to a new destination, reducing partial destination writes from invalid archives. Copy/publish failures can still leave partial output; this is neither atomic nor an OS sandbox and does not activate hook enforcement.

## Git backlink consistency

The worktree check now verifies the linked Git admin backlink, rejecting copied `.git` pointers at a different path. A same-path replacement still passes in a real-Git test; the check is not authorization or a server hook gate.

## Post-command recheck in the checked-runner prototype (not integration)

The standalone checked general-event and pre/post-tool runners now re-resolve after each command, including the final command and a pre-tool hook returning exit 2. Empty/mismatched selections are rechecked before return. Persistent revocation or changed configuration observed at that point raises `HookTrustDenied`; the command and any earlier tool effects already happened and are not rolled back. A real shell-marker test with the process-local review registry verifies revocation after the only general-event command; mocked-command tests cover general, pre (including exit 2), and post paths. Existing checked-runner tests also verify unchanged outcomes. Rechecks cannot stop running processes, detect an unobserved change-and-revert, prevent a change after the final check, authenticate a forged resolver, or make effects transactional. This prototype is still not wired to production server execution, and the review UI must continue to report enforcement inactive.

## User-selected shared-subagent inheritance and prototype (not integration)

The user explicitly selected conditional inheritance: a shared-workspace subagent may inherit the parent's admin approval only for the same owner, exact workspace and exact approved hook configuration; changes or revocation invalidate it. This does not authorize a distinct worktree or arbitrary session. Implementing this requires a trustworthy server-established parent-child relationship, not a client-supplied or mutable session field.

`engine/hook_inheritance.py` is an unintegrated resolver prototype: it checks an approved parent snapshot with nonempty approval identity, separately revalidates child scope and inspected bytes, invokes a trusted shared-lineage validator and repeats the checks. It denies malformed inputs, exceptions, mismatches and observed revocation. Tests use real scoped session records and the process-local review registry, but the lineage is **supplied by the test**. The callbacks are trusted and can forge authority; no production lineage issuer exists in this increment. The checks are not atomic and cannot detect unobserved ABA, prevent post-check changes, stop a running process, or undo effects. Server enforcement remains inactive, and the existing test-only HTTP adapter remains session-scoped.

## Active shared-child lineage tracking (not hook enforcement)

Foreground and background agent execution now record process-local shared-child relationships while `sub.send` is executing, after workspace binding checks. Only scoped stores are recorded; worktree and local/unscoped children are excluded. Records are removed on scope exit, including tested cancellation. Lookups recheck the two owned sessions and their current resolved workspaces; the bounded registry refuses a second active parent for the same owner/child. Issuance failure does not currently block child execution, because there is no server hook gate consuming it.

This is a potential input to, **not proof of**, future authorization: the issuer is a Python API callable by trusted in-process code, not a protected capability, and it cannot authenticate an arbitrary caller. Nothing yet connects this registry to the inheritance resolver or server hook execution. A discarded/never-closed async generator or interrupted cleanup may retain a record until process exit; the registry has a 1,024-entry capacity, no timeout or cross-worker sharing. Scope checks race filesystem changes and do not detect unobserved ABA or same-path replacement. When eventually wired, a missing/failed record must deny inheritance rather than silently fall back to approval. Cancellation does not guarantee background task status or external effects were cleaned up. No process or OS sandbox is provided.

## Opt-in inherited scope resolution through authenticated HTTP (not product enforcement)

An unused server resolver now combines direct session review with conditional inheritance using the active in-process parent record, never a client-supplied parent ID. A child with its own pending/denied review record does not fall back to the parent in the tested stable state; an independently approved child can use its own approval. Parent/child workspaces and ownership, approved bytes, current admin and lineage are rechecked. A fixture-only flag selects this resolver within the existing test-only checked-hook adapter; normal server execution still does not use it.

A real Uvicorn/auth/member/admin test with a fake provider demonstrates: an unapproved parent is blocked by the fixture adapter; after admin approval, a shared child inherits while executing; a later direct chat to that child does not inherit; revoking the parent's review blocks a later parent turn. Five resolver tests additionally cover an explicit child pending/denial, changed config and disabled approver. The existing session-only HTTP characterization remains separately opt-in and unchanged. At this stage there was no HTTP test of revocation during an already-running child hook; the subsequent increment adds one. There is no production gate or coverage of all execution paths. Parent/grandchild recursive inheritance is not implemented: parent resolution uses direct review only. Review records and lineage are process-local and all checks race, including review creation/expiry and filesystem changes. Child review records may be invalidated as stale by existing registry behavior; this is not durable denial policy. Trusted internal issuance remains required and is not a capability boundary.

## HTTP revocation while an inherited child hook is running (test-only)

The opt-in Uvicorn fixture test now waits for a real POSIX child prompt hook to write a marker and block, then uses an authenticated admin HTTP request to revoke its parent's approval. After the test releases the hook, the checked prototype's post-command recheck returns `hook_trust_denied`: the child does not reach the fake provider, and no child-done or turn-end event is reported. The marker confirms the hook's earlier write is not rolled back. This tests a foreground shared child, one general-event hook, a process-local registry, and a fake provider; it does not terminate a running command upon revocation or cover all paths and races. The adapter is fixture-only; server enforcement remains inactive.

## Background-child HTTP inheritance and revocation (fixture-only)

An opt-in Uvicorn fixture now permits the fake provider to request `spawn_background_agent`. In an authenticated test, an approved shared background child runs its `session_start` hook and completes. A second child blocks in that hook while an admin revokes the parent approval; after release, the prototype reports `hook_trust_denied` in the parent's real inbox, and the child never reaches the fake provider. The hook's prior marker write remains. This tests one session-start path with a single process; it does not show revocation immediately kills the command, cover every event or background behavior, or activate production enforcement. Inbox reporting is best-effort and filesystem effects are not rolled back.

## Per-agent lifecycle hook runner injection (not server enforcement)

`Agent` now accepts an optional per-instance hook runner for its lifecycle, pre/post-tool and compaction calls, and passes the same underlying runner to foreground and background subagents. Default callers still use the existing `engine.hooks` module dynamically, including local policy and existing monkeypatch fixtures. An injected runner must provide the four expected callables; its unexpected exceptions or malformed basic result shapes become sanitized `HookTrustDenied` rather than being swallowed by legacy ordinary-error handlers. Cancellation still propagates. Tests exercise two concurrent agents with different runners, normal pre/post dispatch, compaction, both child routes, malformed results and exceptions.

Injection is a mechanism, not authorization: the caller must select a trusted runner, authenticate its owner and session, and ensure it revalidates scope at each boundary. Shape validation cannot prove that a runner enforces policy or detect fabricated decisions. The server does **not** inject this runner yet. Demo, the tool-level global legacy `on_file_change`, HTTP synthetic actions, batch/battle and any other paths outside these lifecycle calls are not covered by this mechanism. A post-hook denial cannot undo a tool's effects. No multi-process consistency or OS sandbox is implied.

## Scoped checked runner injected in HTTP fixture (not production wiring)

`server.hook_runner.ScopedCheckedHookRunner` binds an owner supplied by its caller, delegates lifecycle and pre/post-tool hooks to the checked prototypes and reloads global configuration for each call. It owns a bounded session-start cache. In a new opt-in fixture branch the server's Agent factory injects this runner into actual Agent/subagent execution, without monkeypatching global hook functions. Two authenticated HTTP scenarios reuse the foreground/background inheritance tests, including revocation during a running hook, with the same fake provider. Unit tests cover owner binding, cache rechecks, per-instance cache separation, pre/post delegation and config-load failures.

Only the fixture chooses the adapter; no normal server constructor enables it, and enforcement remains inactive. The owner and resolver are trusted caller inputs, not authentication in the adapter. Per-instance caches can rerun session-start hooks across separate Agent instances; choosing cache lifetime is a production compatibility decision. At this stage global configuration was captured once per adapter call; the subsequent increment reloads it during rechecks. Global legacy file-change hook, demo, synthetic routes, batch/battle, other entry points and process-wide security boundaries remain unaddressed. Review/lineage state is process-local; rechecks race and cannot stop or undo already-running commands.

## Global hook config reloading in checked adapter (not product enforcement)

The checked-runner prototype now accepts either a static config dictionary (legacy prototype callers) or a trusted synchronous loader. The scoped server adapter passes a loader so each selection and recheck rereads global config; a changed selected event hook or loader failure yields `HookTrustDenied`. A real shell hook test modifies the global config and writes a marker: the check detects the persistent hook change after completion, but the marker remains. Mocked tests also cover session start, pre/post tool and unchanged relevant entries.

The config loader's returned data, resolver, and owner remain trusted inputs. Comparing selected entries does not detect unrelated global changes, transient change-and-revert, or changes after a check; hook execution and tool side effects cannot be undone. Cached session-start hits perform a fresh selection but not a post-return check. Static-dictionary callers remain static, and the separate global legacy file-change adapter/path was not changed. This is fixture-only infrastructure: production server hook enforcement is inactive.

## Opt-in checked legacy hook at the actual write/edit boundary (not product enforcement)

`tools.execute_tool` now accepts an optional trusted legacy-hook callback. An Agent with an injected runner supplies it for write/edit; the checked scoped adapter uses the global-only legacy hook prototype and a reloadable config. If the callback denies, fails, or returns a malformed result after a successful file write, the tool result retains the actual write outcome, marks `hook_trust_denied`, warns effects were not rolled back, and the Agent persists/emits that result before stopping without running post hooks. Cancellation propagates. Default/uninjected tool and Agent callers retain the previous legacy behavior.

The opt-in authenticated HTTP fixture test changes a hook-free workspace config into an unapproved hook during a real write: the write remains, but the global legacy marker does not execute; a failed-trust result and denial are emitted and persisted. The separate original test without injected callback still characterizes the old behavior (global marker runs). Real POSIX tests also cover an allowed global hook and a hook that changes the global config while running. Revalidation is best effort; side effects and races remain, and hook configuration does not authenticate referenced scripts or environment.

This is not the production server gate: only the fixture injects the runner, direct library/demo/other callers remain outside it, and synthetic routes, batch/battle, cross-worker decisions, path replacement and cleanup are unresolved. The tool callback is a trusted dependency, not an authorization credential; the actual file is written before this hook check. Legacy runner limits, global/workspace hook semantics and per-instance cache compatibility remain relevant.

## Partial non-demo chat integration (current status)

Normal non-demo, non-synthetic server chat now constructs an owner-bound scoped runner and uses the inherited scope resolver. Its foreground/background subagents inherit the runner. The runner checks lifecycle and pre/post-tool events, and the checked global legacy callback at write/edit; unapproved or invalid workspace hook config denies the protected chat path. Admin approval is revalidated against current owner, session, workspace and exact config bytes; eligible active shared children can inherit one directly approved parent. SSE reports `hook_trust_denied`, preserving prior tool effects where applicable. A test-only fake provider exercises this actual server wiring without monkeypatching hooks or replacing the server Agent constructor.

This is **not complete enforcement**. Demo mode, server synthetic requests (including quick memory), scheduled turns, compact endpoint, batch/battle and direct library/tool execution were not wired by this increment; hook execution through other paths may still bypass decisions. API responses retain `enforcement_active: false` for incomplete coverage and now include `enforcement_scope: normal_non_demo_chat_only`; UI text describes both the checked path and bypass risk. Other gaps include process-local 30-minute reviews/lineage, multi-worker inconsistency, same-path replacement and TOCTOU, mutable referenced scripts, best-effort process cleanup and nontransactional earlier effects. Independent chat Agent instances create separate session-start caches and may rerun start hooks; this is a compatibility change. The check is application-level, not an OS sandbox. No deployment or commit is claimed.

Authenticated real Uvicorn tests cover pending parent, admin approval, shared foreground/background inheritance, revocation while a child hook runs, and a write that installs unapproved hooks before the checked global legacy callback. The demo characterization still shows an uncovered bypass. Four focused HTTP tests passed three runs; the full Python suite passed 778 tests after test-only user mocks were updated with IDs. Three React hook UI tests, Vite build, and real Linux Chromium workspace browser scenario passed; Chromium and OS libraries had to be installed in the test environment. Tests use fake providers and do not establish all-path or multi-worker security.

## Manual compact endpoint integration (current partial scope)

The authenticated manual compact endpoint now constructs the same owner-bound runner as normal non-demo chat, so its `pre_compact` event checks the current scoped admin decision. A real HTTP/Uvicorn/fake-provider test verifies that a pending/unapproved config does not execute its hook or reach the provider, another owner gets 404, an ordinary member cannot approve, an admin approval allows compaction, and revocation denies subsequent compaction. It also revokes approval while a real hook is blocked: after release, denial is returned, conversation messages remain unchanged, and the hook's prior marker remains. This is not immediate termination or rollback. Empty sessions return without invoking pre-compact hooks.

The UI and API scope now include manual compact (`enforcement_scope: normal_non_demo_chat_and_manual_compact`) while `enforcement_active` stays false because demo, synthetic routes, scheduler, batch/battle, direct library calls and potentially other paths remain ungated. Reviews remain process-local/expiring; rechecks race and referenced scripts can change. No OS sandbox, cross-worker consistency or real-provider guarantee is claimed.
