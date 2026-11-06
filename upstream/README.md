# Automated Coq/Iris Proof Agents — Artifact (agent code)

---

## 1. Requirements

### 1.1 A Claude subscription (required)

Running the agents **requires an active Claude subscription**. The harness drives
the model by shelling out to the **Claude Code CLI** (`claude -p ...`); without a
logged-in CLI tied to a paid plan, every run fails at the first model call.

> **Recommended: the Claude Max plan at the 20× usage tier.** A full proof sweep
> issues *thousands* of model calls over many hours. Lower tiers will hit usage
> limits and stall partway through a development; the 20× Max plan is what these
> experiments were run on and is strongly recommended for reproducing them.

Verify the CLI is installed and authenticated before running anything:

```bash
claude --version
claude -p "say hi"        # should reply, not an auth error
```

### 1.2 Python

**Python 3.10+**. No third-party Python packages are required.

### 1.3 A Coq / Rocq toolchain (via opam)

Each benchmark needs an opam switch with its toolchain — see §2. The harness runs
`dune` / `make` and `scripts/coq_check.py` **in the ambient environment**, so you
must launch each agent with the matching opam switch already active
(`eval $(opam env --switch=…)`) before running `main.py`.

---

## 2. Prepare the benchmarks

The target developments are **not bundled**. For each benchmark you (1) clone the
public source, (2) build it in its own opam switch, and (3) strip the proofs you
want the agent to regenerate.

> ⚠️ **Each benchmark needs a different build environment.** The three targets
> compile against *different, mutually incompatible* Coq/Rocq toolchains, so each
> lives in its **own opam switch**. You must activate (or select) the matching
> switch before running a given agent — there is no single environment that
> builds all three. The verifiers for `rustbelt_agent` and `reglang_agent` pick
> their switch from the `LRUST_OPAM_SWITCH` environment variable (defaults below);
> `iris_agent` builds with `dune` in whatever switch is currently active.

| Benchmark | opam switch (default) | Toolchain                                  | Build  |
|-----------|-----------------------|--------------------------------------------|--------|
| Iris      | *(active switch)*     | Rocq + `std++` (Iris dev deps)             | `dune` |
| RustBelt  | `lambda-rust`         | Rocq 9.1.0 + Iris-dev + `std++`-dev        | `make` |
| reglang   | `reglang`             | **Coq 8.18** + MathComp                    | `make` |

### 2.1 Clone the sources

```bash
# Iris
git clone https://gitlab.mpi-sws.org/iris/iris.git

# RustBelt / lambda-rust
git clone https://gitlab.mpi-sws.org/iris/lambda-rust.git

# reglang
git clone https://github.com/coq-community/reglang.git
```

### 2.2 Build each in its own opam switch

Follow each project's README for the authoritative dependency list; the
representative setup is:

```bash
# --- Iris (dune) ---
opam switch create iris 5.3.0          # a Rocq-capable OCaml switch
eval $(opam env --switch=iris)
opam repo add rocq-released https://rocq-prover.github.io/opam/released/
opam repo add iris-dev https://gitlab.mpi-sws.org/iris/opam.git
opam install dune
( cd iris && make builddep && dune build )   # build + install Iris deps

# --- RustBelt / lambda-rust (make) ---
opam switch create lambda-rust 5.3.0
eval $(opam env --switch=lambda-rust)
opam repo add iris-dev https://gitlab.mpi-sws.org/iris/opam.git
( cd lambda-rust && make builddep && make -j )

# --- reglang (make, Coq 8.18 + MathComp) ---
opam switch create reglang 4.14.1
eval $(opam env --switch=reglang)
opam repo add coq-released https://coq.inria.fr/opam/released
opam install coq.8.18.0 coq-mathcomp-ssreflect
( cd reglang && make -j )
```

**Notes on the opam repositories and versions.**

- The commands above are **representative, not authoritative** — the OCaml/Coq
  versions and the exact dependency pins are dictated by each project. Always
  follow the **project's own README** in the checkout you cloned; for the
  Iris-based targets, `make builddep` reads the project's pinned dependency
  versions and installs them for you (it just needs the right repos configured
  first).
- The **released repo** supplies the Rocq/Coq core the libraries build on. Its
  name depends on the era: current **Rocq** (9.x) uses
  `rocq-released` (`https://rocq-prover.github.io/opam/released/`); older **Coq**
  (8.x, e.g. reglang) uses `coq-released` (`https://coq.inria.fr/opam/released`).
- The **`iris-dev` repo** (`https://gitlab.mpi-sws.org/iris/opam.git`) is needed
  only when building the **development/git** version of Iris (or lambda-rust),
  because dev Iris depends on a matching dev `std++` not in the released repo. If
  you instead install a **released** `rocq-iris` package, `rocq-released` alone
  suffices and `iris-dev` is not required.
- The OCaml base-compiler version in `opam switch create <name> <version>` (e.g.
  `5.3.0`, `4.14.1`) just needs to be one the project's dependencies support;
  when in doubt, let opam resolve it rather than pinning a specific number.

> 💡 **If you cannot get a benchmark to compile**, you do not have to debug the
> opam/Coq environment by hand. You can hand the build error to a coding agent —
> e.g. **Codex** or **Claude Code** — and ask it to get the project building in
> its switch (fixing missing repos, version pins, or `_CoqProject` issues). Once
> the *unstripped* project builds cleanly, proceed to strip the proofs and run
> the agent.

### 2.3 Strip the proofs (proof-regeneration protocol)

For every target lemma, **remove the proof body but keep the statement**: replace
the `Proof. … Qed.` block with `Admitted.`. The harness discovers its work by
grepping for `Admitted.`, so a stripped lemma looks like:

```coq
(* before *)
Lemma foo n : P n.
Proof. induction n; eauto. Qed.

(* after — what the agent sees *)
Lemma foo n : P n.
Admitted.
```

Strip the whole file (or the subset you want to evaluate); surrounding
definitions, notations, and all imported dependencies stay intact so they remain
available as context. Keep an unstripped copy elsewhere for scoring. (This step
is a mechanical source rewrite and can be scripted per development; only the proof
bodies of the chosen target lemmas are removed.)

---

## 3. The three agents

The same harness is instantiated as three agents, one per target development:

| Folder            | Target development              | Build system            | Per-file verifier                     |
|-------------------|---------------------------------|-------------------------|---------------------------------------|
| `iris_agent/`     | **Iris** separation logic       | `dune`                  | `iris_agent/scripts/coq_check.py`     |
| `rustbelt_agent/` | **RustBelt / λ-Rust**           | `make` / `coq_makefile` | `rustbelt_agent/scripts/coq_check.py` |
| `reglang_agent/`  | **reglang** (regular languages) | `make` / `coq_makefile` | `reglang_agent/scripts/coq_check.py`  |

---

## 4. Running

Each agent proves the `Admitted.` lemmas in the target checkout, retrying on
failure (up to 30 times) with the Coq error and the goal at the failing point.
Results are appended to a `benchmark_results.log` in the agent folder. A
successful proof prints `[verify_proof: OK]` followed by `Done`.

### 4.1 `iris_agent` (Iris)

The target directory is read from `iris_dir`. Set it to your stripped Iris
checkout (an absolute path is simplest) in the agent's **runtime** configs under
`bin/` — each orchestration stage reads its own copy, so set all three to the
same value:

- `iris_agent/bin/agent.conf`
- `iris_agent/bin/iris_benchmark/agent.conf`
- `iris_agent/bin/iris_benchmark/iris_agent/agent.conf`

Then, with the Iris opam switch active, run from an interactive terminal (the
agent has a pause prompt, so it needs a real TTY):

```bash
eval $(opam env --switch=iris)
cd iris_agent
python3 main.py
```

### 4.2 `rustbelt_agent` and `reglang_agent`

These take the target directory as a command-line argument and select their opam
switch via `LRUST_OPAM_SWITCH` (defaults `lambda-rust` / `reglang`):

```bash
cd rustbelt_agent
LRUST_OPAM_SWITCH=lambda-rust python3 main.py /path/to/lambda-rust-checkout

cd ../reglang_agent
LRUST_OPAM_SWITCH=reglang     python3 main.py /path/to/reglang-checkout
```

### 4.3 Smoke test

Before a full sweep, leave only **one** `.v` file stripped (all others fully
proved). `main.py` always sweeps the whole project, but it will find just that
file's `Admitted.` lemmas — so the run stays short while still exercising the
full build → discover → prove → verify pipeline end to end.

---

## 5. Layout

```
submission/
├── README.md
├── hhl/                         # HHL sources the agent Python is generated from
│   ├── iris_agent.hhl           #   proving agent
│   ├── iris_benchmark.hhl       #   lemma discovery
│   └── iris_project.hhl         #   project driver / build-error analysis
├── iris_agent/                  # Iris agent (dune)
│   ├── main.py                  # entry point (reads bin/agent.conf -> iris_dir)
│   ├── agent.conf               # iris_dir + per-stage prompts / limits
│   ├── iris_agent_ext.py        # hooks: safe_command, read confinement, verify_proof
│   ├── iris_benchmark_ext.py    # lemma discovery / extraction helpers
│   ├── iris_project_ext.py      # build-error analysis helpers
│   ├── scripts/coq_check.py     # dune-based per-file verifier
│   └── bin/                     # generated orchestration code + hooks + settings.json
├── rustbelt_agent/              # RustBelt / λ-Rust agent (make), same design
│   └── ...                      # target dir passed as a CLI argument
└── reglang_agent/               # reglang agent (make), same design
    └── ...
```

---

## Note: the agents are generated from HHL sources

The Python harness under each `<agent>/` folder is **generated from high-level
sources written in HHL (the Harness Hook Language)** — a DSL describing the
agent's control flow, its pre-/post-tool hooks, and its per-stage prompts. The
`hhl/` folder ships these sources for the Iris agent: `iris_agent.hhl` (the
proving agent), `iris_benchmark.hhl` (lemma discovery), and `iris_project.hhl`
(the project driver). The three agents share the same structure and differ only
where their target development does.

The generated Python under `<agent>/` is self-contained and runs **without** the
HHL compiler. **The HHL compiler itself is not uploaded here**; we will make it
public later.
