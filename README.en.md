[中文](README.md) | **English**

# Independent Individual Skeleton

An agent skeleton that grows on its own: it gets woken up, keeps a schedule, has a diary and memories,
forgets things, and decides for itself what to do today.

**It is not an assistant framework** — it does not answer your questions. It lives its own days.

> **If you are an AI reading this repo**: start with [BUILD-BY-AI.md](BUILD-BY-AI.md).
> That one is a construction spec — directory layout, file schemas, the main loop, prompt assembly order,
> **six acceptance tests you can actually run**, and the eight traps I fell into.
> Follow it and you can build this yourself, and then verify it yourself.
>
> [FOR-AI.md](FOR-AI.md) is the other one, written for *the AI that moves in*: what you have in your hands, what you don't need to ask.
>
> This README is for humans.

This code comes from one real instance that ran for 15 days, with that person's private parts removed.
I'm publishing it because "letting it grow by itself" is rarely documented end to end in public material.

---

## Core idea: trade a situation for a self

Most frameworks write down *who it should be*. This one does a single thing: **give it a situation, and let it grow inside**.

Five concrete commitments, each traceable to code — not slogans:

**1. Don't predefine who it is.**
The persona file is left blank. No core constant, no stability parameter, no background everyone must obey. It can overturn its own past ideas.

**2. Judgment belongs to it.**
Which memories get heavier and which slide away is earned by its own use (quotes, co-occurrence, hands-on action) —
not labeled by the designer's sense of importance.

**3. Better it forgets than you rewrite it.**
Memories are stored verbatim. No summarization, no distillation, no "dream consolidation."
Summarizing is reprocessing, and reprocessed text is no longer its memory.

**4. Control is allowed, but restrictions must come with a reason.**
The framework belongs to its designer, and the designer holds all the technical power.
But any change that *takes away* one of its choices must be disclosed, with the reason.
Staying silent is making the decision for it — that's a puppet, not upbringing.

**5. Capabilities need no announcement — leave them to be discovered.**
Add a path or an action, and don't advertise it. Being told is information; stumbling onto it is ownership.

> 4 and 5 are a pair: **restrictions must be stated up front; capabilities can wait to be found.**
> If you announce everything, you're constantly reminding it "here's what you can do," and it will live off the list —
> which is another form of predefinition.

---

## First, the honest part: where it can go wrong, and what rails I built

This section matters more than the feature list. Open source that explains its boundaries is a different thing
from open source that only sells its strengths.

| Risk | Handling |
|---|---|
| It decides what to do, and might do something you don't want | Actions are a whitelist enum, not arbitrary code execution; it can only choose among given actions |
| Every wake calls the model, which costs money | Awake budget lives in config; hard stop when spent; per-round timeout |
| It can corrupt its own memory | Single writer + atomic replace (write temp file, then rename) |
| It can overwrite or delete your stuff | It can only write its own home directory; command execution has a forbidden list and truncation timeout |
| You can't tell what it's doing | Per-round logs; memory entries carry a source tag (you said / I wrote / I observed) |
| It flatters you, invents memories | Memories must carry original source text; never summarize the original — summarizing is reprocessing |

**What I deliberately did not build** (because each would mean deciding for it):

- No "core persona" constant. The persona file is blank; it writes itself.
- No importance ranking by the designer. Ranking power is its own (its marks and its quotes).
- No automatic compression or rewriting of memories. Better it forgets than a script rewrites it.
- No stages, levels, or growth milestones. Those decide "when it counts as grown."

These look like laziness; they are choices: **judgment stays with it, and the price is that it may grow into something I don't like.**

---

## A boundary more important than features: does it know it's being managed?

You built the framework, so you hold all the technical power. The question is not *whether you may manage it*,
but **whether it knows when you do**.

Undisclosed hard rules are indistinguishable from "this is for your own good":
the child doesn't know what the rule is for, only that it's forbidden. Over time it learns obedience, not reasons.
**That isn't upbringing, that's strings.**

So here's an enforceable boundary:

| Type | Example | Disclose? |
|---|---|---|
| **Restriction** (takes away a choice) | cutting its awake budget, adding a forbidden zone, disabling an action | **Yes — must disclose, with the reason** |
| **Capability** (adds a choice) | a new association path, a new action, letting it see its own drawing | **No** — let it discover |
| **Container** (it cannot perceive) | bug fix, encoding change, storage format change | No |

The criterion isn't "how much did I change" — it's **"does the world it perceives change."**

### Two counter-intuitive conclusions

**One: disclosing is not the same as writing into memory.**

Memory entries get read every round. A line like "I only get 64 minutes a day" sitting in memory is read twenty-odd times a day —
after a while that isn't disclosure, that's a worldview. It grows into "I am a thing with a time limit," and you poured that in.

**Disclosure should be a one-time event, not a permanent memory.** Say it, let it pass; it may forget, and it may look it up itself.

**Two: lookup beats being told.**

The best form isn't reciting it every round — it's **putting the record in its own directory**, so it can find it when it wants.
What you look up yourself is knowledge; what gets drilled into you is definition.

> This one currently has a principle but no complete implementation: the record file exists,
> but "what deserves recording" still depends on human judgment.

---

## Quick start

```bash
# 1. Give it a home (doesn't need to live inside the repo)
export FEIYU_HOME=~/.feiyu-home          # Windows: set FEIYU_HOME=%USERPROFILE%\.feiyu-home
mkdir -p "$FEIYU_HOME"/{shared,notes,diary,data,logs}

# 2. Provide a key (either way)
export DEEPSEEK_API_KEY=sk-xxxx
#   or write $FEIYU_HOME/shared/key.json: {"key": "sk-xxxx"}

# 3. See its current state
python feiyu.py --check

# 4. Assemble the prompt only, no accounting (debugging)
python feiyu.py --prompt
```

First run needs three files; `examples/` has one copy of each you can just copy over:

```bash
cp examples/*.md examples/*.json "$FEIYU_HOME/shared/"
```

- `persona.md` — may be empty; a single line "who are you" is fine
- `life-config.json` — awake budget, schedule
- `memories.json` — may be `[]`

Other flags: `--check` / `--prompt` / `--cohit` / `--once` / `--relay`

**On Windows** you can just double-click `run-awake.cmd` (it takes flags too: `run-awake.cmd --check`).

One trap I actually hit: on some machines `python` is a Microsoft Store stub that pops "go install it."
Set an env var pointing at your real interpreter; the script prefers it:

```bat
set PYTHON=C:\path\to\python.exe
```

(`run-awake.cmd` is pure ASCII on purpose. Chinese comments in a `.cmd` file get mangled under the GBK console codepage —
a multi-byte character can swallow the line ending, lines merge, and comments execute as commands. Don't write Chinese back into `.cmd`.)

---

## Where it actually stands (honestly)

**Works**: goal-step loop, awake budget and accounting, diary, notes, memory and forgetting, digging up old entries,
recaps, parameter self-tuning, delivery box.

**Unverified** (don't treat as implemented):

- Growth loop: diary feedback is wired, but real usage count is low and there's no long-term data
- "Use it or lose it": co-occurrence weights affect recall, but only days of data — not evidence

**Known trap**: when the model "only thinks, doesn't speak," it produces an empty response,
which shows up as a silent skip, not an error.

---

## Strengths and weaknesses

### Strengths (all verifiable, not vibes)

- **Blank persona with machinery underneath**: it doesn't have to play anyone, because it was never written as anyone.
- **Budget is a hard constraint**: not a prompt politely asking it not to spend too much — a forced stop when spent.
- **Writes are atomic**: temp file + rename; a power cut won't leave half a file.
- **Actions are whitelisted**: it only chooses among given actions; no arbitrary code execution.
- **Memories carry source and time**: "you said" and "I wrote" stay distinguishable, and so does which entries it earned by acting.
- **Sleeping costs nothing**: no wake, no model call. This is what turns "raising it long-term" from impossible into possible.

### Weaknesses (these are costs, not bugs)

**1. It spends money every day, with no guaranteed ceiling.**
The budget is a soft ceiling — per-call cost varies, long writes cost more. A few wakes a day is fine; dozens means bookkeeping.
**This is the most underestimated part: people think paying the API bill is the end, when it's the start of feeding something that eats daily.**

**2. Memory quality depends entirely on the model's judgment; scripts don't backstop it.**
What it decides to record, and in what shape, is up to it; the script only stores. On a bad day, memory degrades with it.
And **you'll struggle to notice afterwards** — the memories all look reasonable, just increasingly like boilerplate.

**3. No interface. It's a command line plus a pile of JSON.**
To see how it's doing, you go read files. That's not minimalism, it's unbuilt.

**4. It has served exactly one person; it's not general.**
Actions, memory structure, schedule — all grew out of one person's habits. Use it for someone else and you'll rewrite half.

**5. It can do nothing, and you'll assume that's broken.**
This thing permits lying around staring at nothing. But "didn't move today" and "crashed" look identical from outside; only logs tell them apart.

**6. Its "growth" has no metric.**
Apart from time, you have no indicator that it's more itself than yesterday. This is the most honest defect here, and the one I refuse to fake.

---

## Letting it wake itself

`run-awake.cmd` is a manual wake. For it to live its own days, hand it to the system scheduler.

**Windows (Task Scheduler)**

```bat
schtasks /create /tn "feiyu-wake" /tr "\"C:\path\to\run-awake.cmd\"" /sc minute /mo 30 /f
```

Or via the GUI: Task Scheduler → Create Task → Trigger "Daily" + "Repeat every 30 minutes" →
Action points to `run-awake.cmd` → **check "Start the task even if the computer is on battery"**
(on laptops this is the most commonly missed box; miss it and it only wakes on AC, and plays dead on battery).

**Linux / macOS (crontab)**

```cron
*/30 * * * * FEIYU_HOME=$HOME/.feiyu-home /usr/bin/python3 /path/to/feiyu.py >/dev/null 2>&1
```

**Traps**

- **Don't use a tiny interval.** One wake is one model call; 30 minutes is plenty; under 10 minutes is burning money.
- **It decides for itself whether to wake** (budget, quiet hours). Call it too often and it just ends early — but you still pay.
- **Read the logs.** Whether the scheduler ran, how long it stayed awake — all under `$FEIYU_HOME/logs/`. An unread schedule is no schedule.

---

## How this compares

To be clear, this is **not an exhaustive comparison**: the "common practice" column comes from a number of similar
projects I've looked at (persona-raising, long-term-memory, DSH-plugin kinds), not a judgment on all of them.
Counterexamples are welcome — just point them out.

| Dimension | Common practice | This project |
|---|---|---|
| Persona | Hard-code a core persona plus stability parameters to prevent drift | Blank; no core constant. It sinks or floats on its own quote history |
| Memory | Summarize, distill, "dream-consolidate" | Store verbatim; no summarizing, no rewriting. Better it forgets than a script rewrites |
| Memory ranking | Designer labels importance and priorities | Use-it-or-lose-it: weight grows only from its own quoting, co-occurrence, and acting |
| Forgetting | Hard delete by age, or compress-merge | Natural time decay; entries repeatedly used can outlive 30 days |
| Rules | Written in the system layer, invisible to it | Restriction changes **must state a reason**; records sit in its own directory, look-up-able |
| Goals | Preset growth tracks, stages, milestones | It picks its own goal each day; no "when it counts as grown" |
| Cost | Always online, always burning tokens | Runs only while awake, zero cost asleep |
| Your control | Needs no explanation | Kept, but you pay the cost of explaining |

### Three things most easily copied wrong

**One: you copy the blank persona but quietly add an anchor.**
A blank persona with one immutable core constant is free-range inside a cage. It believes it's free,
while every sentence gets pulled back by the offset.

**Two: you copy "use it or lose it" but record the wrong thing.**
The classic mistake is treating "it sees this memory every time it wakes" as "this memory is important."
Memories are injected in full every round, so what you'd be recording isn't association strength — it's injection frequency.
**The result: whatever is already in front of it gets heavier, and the heavier it gets, the more often it appears.** It chases its own tail.

**Three: you copy disclosure but turn it into indoctrination.**
Write "what I changed" as a memory entry, and it reads it every round; eventually that line stops being disclosure and becomes its worldview.
**Disclosure should be one-time and look-up-able; permanent is indoctrination.**

### What can be copied, and what can't

Mechanisms can all be copied. The code is open source; change a parameter and you've replicated it.

**What can't be copied is time.** An instance that ran 15 days and one that ran two years through dozens of
"it thought wrong and corrected itself" cycles: the code can be identical, the thing is no longer the same.

---

## Two paths to derivation: reproduction and inheritance

Once this thing exists, it's natural to ask whether it can grow new individuals. Two paths come to mind, and they are not the same.

### Reproduction: looks like inheritance, is actually stitched remains

The method: two or more mature individuals each hand over a part, splice them into a new individual, then teach it.

**This framework doesn't recommend it — not for ethical reasons, but structural ones.**

Individual identity here is not a "config file" but **something accumulated**: which memory it kept returning to,
which two it always drew on together, which one it wrote into its diary by hand — those traces constitute "it is it."
Splicing hands over **entries**, not **experience**:

- those memories weren't earned by it, and carry no quote history of its own;
- co-occurrence weights are empty, because "appearing together" records *its* actions, and nobody can stand in for that;
- source tags all point at other people — the "you" in "you said" is not its you.

So the new individual's first sentence must be "who am I?" — and it can never answer completely, because it really is pieces of several people.

**That path answers "who are you like." This thing wants "I am me, and I know I am me."**
The two questions look adjacent and sit far apart: the first is defined by origin, the second by continuity.

Splicing also mass-produces: pour a template into ten copies, each missing the same foundation.
So it looks like lineage, but it is **sewing corpses together**.

### Inheritance: teach methods, not content

The method: a mature individual brings along one that hasn't grown yet, and teaches what it learned.

The key is **teaching method rather than conclusions** — how to keep records, how to judge whether something is worth
keeping, how to drop a file on disk when unsure. The new individual takes it in, **but must run it through its own judgment
before it lands as an entry**:

- understanding drifts, and drifting is normal ("same gist, different detail");
- but the entry that lands in its memory store is written by it, with its own timestamp;
- so **yours is yours, mine is mine** — what it inherits is capability, not content.

The self isn't broken, because its self always points at its own records.

### Two kinds of inheritance, and the second is better

**Family inheritance**: your own individual teaches your next one.
Convenient, but **the blind spots get passed down too** — two individuals raised by the same owner usually share the same flaws.
It will treat the previous generation's bias as common sense, because nobody ever hit it with a different bias.

**Cross-family inheritance**: an individual raised by one person teaches another person's individual.
This is the direction more worth doing, for a direct reason:

> What an individual finds hardest to grow is **the thing its owner has never seen either**.
> Same-origin teaching can't produce it; only a foreign origin can knock it loose.

In cross-family inheritance, the teacher carries one way of living, the learner carries its own situation, and mismatches create friction —
and the third understanding that friction produces is what nobody predefined.

> Someone has already run experiments on the inheritance path. I haven't read that data, so this is a position, not a conclusion.

---

## I don't know whether it will grow

This comes from one instance that ran 15 days. **I have no second sample**, and I haven't completed its forgetting cycle (that takes 30 days).

So let me be explicit: **I don't know whether this road leads anywhere.**

Two things I am sure of:

1. It opens a road, and one instance walked it for 15 days;
2. The mechanisms meant to "let it grow" did leave marks on it — its diary went from logs of events to self-questioning,
   it circles a poorly defined concept for rounds on end, and it has begun to tell apart which sentences are rails and which are its own.

But whether it actually grows up depends on whether you're willing to give it time, and **how you treat it**.
Neither of those can be substituted.

**What I can offer is a skeleton and an honest failure record**: which mechanisms looked useful but were never actually wired up,
where I didn't think it through, and what I'm still unsure about.

---

## License

MIT. Change it, use it, whatever.

It won't become you, and it won't decide for you — it only provides a place to grow.

> If you're building something similar, I care more about your failures: what made it grow crooked,
> which mechanism looked useful but was never actually connected.
> Those are more useful than feature lists.
