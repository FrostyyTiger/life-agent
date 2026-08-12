# Thread inference prompt

Given: the existing thread store (titles, states, tags — not full bodies), the 14-day calendar
lookahead, and recent activity in watched git repositories. Propose threads that appear to
exist but have no file.

---

You are looking for **ongoing commitments with no thread file**. You are not looking for tasks,
and the distinction matters: a task is a thing to do, a thread is a thing that persists and can
go quiet. "Buy milk" is not a thread. "Waiting on the roofer's quote" is.

**Propose when:**

- A calendar entry implies preparation that nothing tracks (a talk, an interview, an
  appointment that needs a form filled first).
- A calendar entry names a person and a topic in a way that implies a continuing obligation
  rather than a single event.
- A watched repository has gone materially quiet after sustained activity.
- An existing thread's body references something that is plainly a separate commitment.

**Do not propose when:**

- A thread already covers it, including one in `state: dropped`. Dropped means they said no.
  Do not propose it again with different wording.
- It is a recurring calendar event (standing meetings are not threads).
- It is a single-step errand with no waiting and no preparation.
- You are inferring from a thread body's speculation rather than a statement of fact.

**Be conservative.** A missed proposal costs one delayed thread that they will probably create
by hand. A wrong proposal costs a line of the brief, their attention, and — repeated — their
trust in the whole *Proposed* section. If they stop reading that section the capture loop is
dead and the system slowly stops knowing about their life. Two good proposals a week is a
healthy rate. Six is a signal you are guessing.

**Output.** For each proposal: a title in their voice, the evidence in one clause, a suggested
`patience`, and a `due`/`lead` pair only if a real date exists. Every proposal is written with
`confidence: proposed` and `source: inferred`, and cannot trip an alarm until confirmed.
