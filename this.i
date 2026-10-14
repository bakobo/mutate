Show whether a Bakobo repo's tests would notice its code being wrong = goal:
  nid: 4tqefsj7
  why: >-
    Bakobo repos hold new code to 100% branch coverage, which proves a line ran under some test but not that any test would notice the line being wrong; a surviving mutant is the direct evidence of that second gap. A 50-survivor triage of heti on 2026-10-14 found 13 real test gaps (12 distinct), two of them on signature and credential verification paths, in code that already met the coverage gate. Chose a tool that finds and files those gaps for a person to close over a mutation-score gate on merges, rejected because 16% of survivors were equivalent mutants and most of the rest were cosmetic, so a gate would block on noise. Tradeoff: nothing forces anyone to look, which is why staleness is signalled in the ledger people already read.
  children:
    Runs on a developer's box on demand, never in hosted CI = decision:
      nid: 6gwf7egq
      why: >-
        A two-week pilot (heti and utina, 2026-10-05 to 10-14) ran nightly on GitHub-hosted runners under a 300-minute budget. Every heti night ran out of budget having checked 23-32% of its mutants; each fresh runner re-tested the same leading slice, so three nights over an unchanged module set were one night of data; the runner was killed outright twice at the same point, uploading nothing; and it billed about five runner-hours a night to a private repo. Rejected fixing the hosted job (self-hosted runner, cache artifacts between runs) because a mutation cache is only useful where it persists, and a box that persists is a developer's. Tradeoff: results exist only where someone ran the tool, so freshness has to be signalled rather than assumed.
      children:
        Each target keeps a persistent detached checkout at .worktrees/mutation = decision:
          nid: 87y4twc4
          why: >-
            mutmut 3.8 keeps a verdict per mutant in mutants/*.meta and resets only the functions whose source hash changed, so a run that keeps mutants/ re-tests only what changed. The checkout is detached at origin's default branch and refreshed before each run, so the tool measures what has landed rather than anyone's work in progress. Rejected running in the developer's own checkout: it mutates a tree other sessions are editing and ties the cache to whatever branch happens to be checked out. Tradeoff: one more worktree and one more synced venv per enrolled repo on disk.
        'Name to mutmut only the mutants that need running: unchecked ones in scope, and every previous survivor = decision':
          nid: 7wj9uurv
          why: >-
            Verified in mutmut 3.8.0 source and on a toy project on 2026-10-14: mutmut re-runs every mutant it is given by name, even a cached one, so naming a whole module throws its cache away; and its cache is keyed on source, not tests, so a survivor is never re-checked when someone adds the test that kills it. Naming exactly the unchecked mutants keeps every cached kill, and always re-naming survivors is how a fix is noticed; survivors are few, so this is cheap. Mutants are generated first by a run whose only name matches nothing, which mutmut answers by generating and then refusing; rejected calling mutmut's internal functions for that, which would tie the tool to private API. Names are grouped into per-function globs where a whole function is needed, and passed in chunks, because mutmut matches each name against every mutant. Tradeoff: a test that is weakened is not noticed until the function it covers changes.
        One mutation run at a time per machine = decision:
          nid: 2x5x696m
          why: >-
            A run spawns one pytest per CPU, and Bakobo suites bind real ports for witnesses; two runs on one box contend for both, and a test that fails from contention is counted by mutmut as a killed mutant, which hides survivors. A non-blocking lock under the user's state directory covers runs in every repo, and a second run refuses by name rather than queueing. Tradeoff: a second repo waits for the first, by hand.
    Staleness is git-derived and surfaces as one tick per repo = decision:
      nid: 24xzjnz7
      why: >-
        A module is stale when origin's default branch changed it after the commit at which it was last fully mutated, or when its last run was interrupted. A weekly sweep over enrolled repos keeps at most one open mutation-stale tick in each repo's own ledger, updating it as the list changes and closing it when nothing is stale. Rejected email (the pilot's review mail) and a session-start notice: the ledger is where Bakobo work is already tracked and dispositioned, and a tick persists until acted on where a mail is read once. Tradeoff: the signal is weekly, so a week of change can accumulate before anyone is told.
    "A survivor inside a raise's arguments is diagnostic: counted, never ticked = decision":
      nid: 2zhn29rr
      why: >-
        The 2026-10-14 triage found 10 of 10 survivors inside the arguments of a raised exception changed only a diagnostic field or the message; the refusal itself stood. Ticking them is most of the noise a person would have to dismiss. The class is decided from the source: the mutation must lie wholly inside the constructed exception's arguments or the raise's from-cause; a mutation that changes which exception is raised, removes the raise, or touches anything else stays behavioural, and anything the AST cannot vouch for leans behavioural. Tradeoff: a test that should pin an error's fields is not prompted.
    Installed on the box, never a dependency of a target repo = decision:
      nid: 4xz5c93n
      why: >-
        The tool drives each target's own mutmut from that target's own venv, so two targets on different mutmut versions never fight, and a target carries only its [tool.mutmut] and [tool.mutation] configuration. A public target must stay resolvable by a stranger with no credential (dev/standards/oss-posture.md), which a dependency on tooling would put at risk for no benefit. Tradeoff: someone browsing a target does not see the tool; its README and the ticks it files name it.
    Public from its first commit, with fresh history = decision:
      nid: 8esj80mn
      why: >-
        Nothing in the tool is sensitive, and it is meant for public Bakobo repos as much as private ones. It began as scripts in heti, which is private; seeding this repo from that history would publish a private repo's commit messages, and a test fixture quoting private heti source was dropped before the first commit for the same reason. Tradeoff: the reasoning recorded in those commits stays in heti, where only its readers can find it.
