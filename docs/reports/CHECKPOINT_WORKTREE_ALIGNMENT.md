# Checkpoint / worktree alignment: investigation and migration plan

Status: **no key migration implemented**; a standalone current-workspace base capture prototype now exists but is not integrated. This document accompanies the real-Git characterization in `tests/test_checkpoint_worktree_key_characterization.py`.

## Observed behavior

- `tools.execute_tool` takes a workspace-keyed checkpoint **before** `write_file`/`edit_file` and several other mutating calls. `checkpoints.key_for` hashes the string form of the workspace path (12 hexadecimal MD5 characters); it does not canonicalize aliases or include the owner or session. The resulting `forge` ref is part of the undo stack, not an assertion of the present workspace contents.
- `Agent._prepare_subagent_worktree` passes its session ID to `worktree.make`, whose repository lookup is session-keyed. With only a normal tool checkpoint, creation fails closed. A direct real-Git test shows that merely calling `make` with the workspace key would fork the **pre-write** tree rather than the current post-write workspace.
- `SessionStore.fork` shares an existing workspace pointer; otherwise it tries a session-keyed archive and then falls back to `copytree`. In the exercised default-workspace case, a workspace-keyed tool checkpoint does not prevent the copytree fallback; it copied the current post-write file. If an older session-keyed snapshot exists, the archive path instead returned that older file. This documents current behavior, not an endorsement of fork freshness or atomicity.
- Two sessions pointed at the same exact workspace path have the same checkpoint key. Conversely, a symlink alias and its resolved directory produced different keys despite referring to the same directory. These are local tests, not proof of authorization across users.
- Agent diff, REST/chat and CLI/TUI merge/discard currently use parent session IDs. Worktree listing derives a `src` token from the Git common directory name. At investigation time server chat merge created a session-keyed snapshot *after* applying the patch while the REST merge path did not. This has since been replaced by a shared pre-apply workspace-keyed checkpoint in `worktree.merge`; see the progress report. Undo in CLI/TUI/server uses the workspace key. Changing only creation would leave management broken, alter identity checks and potentially introduce false undo assumptions.

## Design requirements before implementation

1. Define the fork point explicitly: latest pre-operation undo snapshot versus a separate capture of current workspace. Do not present the former as current. Capturing current files is not automatically atomic while concurrent writers exist; document exclusion rules, errors and consistency limits.
2. Keep parent/child identity and workspace authorization distinct from a repository key. Sessions sharing a workspace key are not necessarily parent/child. Record and validate an owner-scoped parent, child, workspace identity, repo identity and fork point before management actions; consider restart, deleted sessions, changed pointers and concurrent operations. Do not rely solely on a mutable `.git` file or a short hash as authorization.
3. Do not silently migrate, overwrite, merge or delete session-keyed or workspace-keyed histories. Existing worktrees need an explicit, validated compatibility path; ambiguous or invalid bindings must fail closed without destructive cleanup. Define behavior when both repo variants exist, when the workspace path changes, or when Git/metadata is inaccessible.
4. Keep fork capture and undo semantics separate unless a deliberate, tested decision proves stack behavior. In particular, replacing the undo ref with a current-state capture would add/change undo entries; an after-merge snapshot cannot be assumed to restore the pre-merge state. Ensure Git index isolation, refs, cleanup and bounded execution are covered.
5. Update creation, background and foreground dispatch, diff, list, merge/discard, all REST/chat/CLI/TUI entry points, and session fork deliberately. Do not conflate a repo key printed in `src` with an authorized parent session ID. Specify compatibility for external callers of `worktree` functions.
6. Test real tool writes then worktree creation, foreground/background binding, snapshot freshness, isolated changes, diff, merge and discard, undo before/after merge, existing session-keyed worktrees, both repo types, missing/malformed metadata, wrong parent/owner, shared workspace, path aliases, restart, and injected Git failures. Add authenticated HTTP tests and verify local CLI/TUI behavior. Race/concurrency and platform limitations must remain explicit.

A safe migration mechanism and exact snapshot semantics have **not yet been implemented or proven**. The existing worktree is not an OS sandbox. Server workspace hook enforcement is still inactive; this investigation neither changes approval scope nor authorizes inheritance.

## Separate base capture prototype

`engine/worktree_base.py::capture` reserves a distinct child-keyed Git repository and captures the current workspace without advancing the parent's workspace-keyed undo ref. Real-Git tests establish the basic mechanism, collision behavior, exclusion handling and selected failure/cancellation behavior. It is not used by production worktree creation. It neither records an authorized parent/owner link nor solves management, legacy compatibility, cleanup, concurrent writes, untrusted Git configuration or crash recovery. Failed/partial repositories deliberately remain and must not be silently reused. The prototype has no production readiness or atomic snapshot claim.

## Live binding consistency prototype

`engine/worktree_binding.py` validates a caller-trusted in-memory record against scoped sessions, workspace pointers and Git refs. It does not authenticate or persist that record and is not wired into product operations. A regression test intentionally demonstrates that a forged parent in the same owner and workspace can pass. Therefore it cannot establish original parentage or authorize merge/discard on its own; a trusted issuer and durable storage with a recovery/lifecycle design are still prerequisites.

## Experimental persistent binding registry

The unused SQLite registry in `engine/worktree_binding_registry.py` preserves a caller-supplied record across reopening and a fresh process, prevents API replacement of the same child, and separates missing from errors. It trusts its writers and filesystem: an accepted forged issuer record is stored, and DB modification or replacement can also forge history. It is not a trusted issuer, access-control system, crash-consistent transaction with Git/session creation, audit log, production integration or migration. Live validation remains separate and raceable.

## Experimental scoped issuer

The unused `engine/worktree_issuer.py::issue` creates the child itself under a caller-provided scoped store, captures the current parent workspace to a separate repo, creates and validates the worktree, and records the relationship. Direct real-Git tests cover its happy path and several failures, including a record remaining after a post-publication mismatch or discard. This is not a production entry point or an independently authenticated issuer. Setup and publication are not atomic and may leave partial artifacts; registry trust, recovery, management integration, legacy coexistence and concurrency remain open.

## Direct worktree identifier boundary

Public worktree helpers now reject malformed ID/repo keys before constructing paths or invoking Git, using the SessionStore exact-ID grammar; this includes the `wtbase-...` experiment. This is syntax validation, not authorization or a fix for key alignment. Direct checkpoint key syntax is now checked; worktree diff now binds its optional path to the expected child directory. Caller authorization and filesystem/Git races still need review. Worktree creation now refuses observed preexisting targets, but its checks are not atomic.

## Worktree creation collision safety

`worktree.make` no longer attempts forced removal or reuse of a preexisting target; it also rechecks after its Git availability await. This avoids the tested dirty-worktree loss on a repeated request. The checks are not atomic with Git creation; failed creation can still leave artifacts, and cleanup/recovery is not automatic.

## Direct checkpoint key checks

Checkpoint repository path helpers and snapshot/undo/list entry points now reject malformed keys before Git or repo creation. This preserves valid workspace hashes and session-style keys but is not an owner/workspace binding or a key migration.

## Worktree diff path binding

Diff now refuses an optional directory that differs from the expected child worktree, preventing a copied Git pointer in an impostor directory from passing the observed repo/branch check and staging its files. Diff and merge reject symlinks at the final expected worktree path. The checks race later Git operations and are not authorization or sandboxing.

## Textual Git runner prerequisite

`worktree._run` now uses bounded merged-output capture and best-effort direct-child cleanup shared with checkpoint execution. Overflow or timeout can follow partial Git side effects and do not imply rollback. Binary merge diff/apply and both archive capture paths are now bounded separately.

## Binary merge diff prerequisite

Merge now drains raw diff stdout/stderr with independent limits and refuses overflow instead of applying a truncated patch; best-effort direct-child cleanup covers timeout/cancellation. Staging may already have changed the child index. Apply was subsequently bounded; archive/export remain separate unbounded paths, and no OS sandbox or rollback is implied.

## Binary merge apply prerequisite

Apply now streams bounded patch input while draining capped stdout/stderr, with a post-spawn timeout and best-effort direct-child cleanup. Errors warn of possible partial workspace changes; neither a successful write to a pipe nor a checkpoint proves full consumption or rollback. The synchronous archive/export path remains unbounded.

## Asynchronous archive capture

Async `export_tree` now rejects archives exceeding 16 MiB before extraction using the bounded binary runner. The separate sync archive used by `SessionStore.fork` has since been bounded; its fallback remains significant. Extraction itself is not transactional and may leave partial output.

## Synchronous archive capture and fork fallback

`export_tree_sync` now caps retained Git archive/stderr output and uses best-effort direct-child timeout cleanup. At that stage `SessionStore.fork` silently fell back to live copy on archive failure. It now fails when a session-keyed repo exists but export fails; the no-session-repo fallback still copies live files. Copying and extraction are not atomic. Descendants retaining pipes can outlive the bounded join and keep daemon reader threads/streams alive.

## Fork failure contract

Fork now fails and reports the partial child if an existing session-keyed repo cannot be exported, rather than silently copying live state; pointer-copy failures also fail. REST reports 409, CLI/TUI report the error. If no session-keyed repo exists it still copies live state, even if workspace-keyed checkpoints exist. Partial sessions/files can remain, and extraction/copying are not atomic.

## Staged archive extraction

Sync and async archive exports now extract into a temporary sibling first and copy to a new destination only after extraction succeeds, rejecting existing destinations. This protects the tested destination from an invalid/truncated archive but publication via copytree can still fail partway and leave files; cleanup, filesystem races, disk consumption and crashes remain limitations.

## Git worktree backlink consistency

The shared worktree validator now checks Git's administrative backlink and rejects a copied `.git` file at a different directory. A real-Git test demonstrates that replacing a directory at the same path with a copied pointer still passes. Backlinks are mutable path metadata, not trusted identity, authorization or a sandbox; races and legacy compatibility remain.
