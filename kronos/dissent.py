"""Mandatory review before answering (moat 11.4).

Ownership makes one agent the authority on a topic, and an authority nobody
contradicts is how a swarm of six converges on one blind spot. When the owner
declares `dissent: require`, its draft answer is shown to an agent with a
different role first — the objection reaches the user together with the answer,
not three days later.

Design constraints that shaped this:

* **Opt-in.** `dissent: allow` (the default) makes every function here a cheap
  early return, so nothing changes for a swarm that does not ask for it.
* **No new poller.** The reviewer side rides the council intake pass, which
  already polls a shared queue every 30s and answers with the agent's own
  expertise. A challenge is a one-participant council with the opposite exit:
  a council ends in synthesis posted to the chat, a review ends in a verdict
  handed back to the author.
* **A timeout must not eat the answer.** If nobody reviews in time, the answer
  is sent with a visible "не прошло ревью" mark. Silence from a reviewer is not
  a reason to leave the user with nothing.
"""

import asyncio
import logging

from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.dissent")

# How long the author waits. The reviewer is picked up by a 30s poll and then
# has to think, so anything under a minute would time out most of the time.
DISSENT_TIMEOUT_SECONDS = 90
DISSENT_POLL_SECONDS = 3

UNREVIEWED_MARK = "⚠️ Отправлено без ревью — коллега не ответил вовремя."

AGREE_PREFIX = "согласен"


def pick_reviewer(profiles: dict, author: str) -> str:
    """Who reviews this agent's answers.

    Prefers the escalation counterpart — the agent already designated as this
    one's cover, so the pairing stays declared rather than random. Falls back to
    the first other agent by name for determinism.
    """
    profile = profiles.get(author)
    counterpart = profile.escalates_to if profile else ""
    if counterpart and counterpart in profiles and counterpart != author:
        return counterpart
    others = sorted(name for name in profiles if name != author)
    return others[0] if others else ""


def classify_verdict(response: str) -> str:
    """ "agree" when the reviewer had nothing to add, else "challenge"."""
    head = response.strip().lower().lstrip("*_ ").strip()
    return "agree" if head.startswith(AGREE_PREFIX) else "challenge"


def format_reviewed_answer(answer: str, *, reviewer: str, verdict: str, response: str) -> str:
    """An objection is information the user needs; agreement is not.

    So agreement changes nothing visible — the absence of the unreviewed mark is
    what says "this went past a second pair of eyes".
    """
    if verdict != "challenge" or not response.strip():
        return answer
    return f"{answer}\n\n⚖️ Возражение от {reviewer}:\n{response.strip()}"


async def review_before_send(
    *,
    answer: str,
    chat_id: int,
    topic_id: int | None,
    thread_id: str,
    root_msg_id: int,
    topic: str,
    author_agent: str,
    timeout: float = DISSENT_TIMEOUT_SECONDS,
) -> str:
    """Return the answer to send, possibly carrying a peer's objection.

    Only called when this agent owns the topic; returns the answer untouched
    unless the owner's profile requires dissent.
    """
    from kronos.swarm_config import all_profiles

    if not answer.strip():
        return answer

    profiles = all_profiles()
    profile = profiles.get(author_agent)
    if profile is None or profile.dissent != "require":
        return answer

    reviewer = pick_reviewer(profiles, author_agent)
    if not reviewer:
        log.info("Dissent required for '%s' but there is no other agent to ask", topic)
        return answer

    swarm = get_swarm()
    challenge_id = swarm.request_challenge(
        chat_id=chat_id,
        topic_id=topic_id,
        thread_id=thread_id,
        root_msg_id=root_msg_id,
        topic=topic,
        author_agent=author_agent,
        reviewer_agent=reviewer,
        claim=answer,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        row = swarm.get_challenge(challenge_id) or {}
        if row.get("state") == "answered":
            swarm.incr_metric("dissent_reviews_agree" if row["verdict"] == "agree" else "dissent_reviews_challenge")
            return format_reviewed_answer(
                answer,
                reviewer=reviewer,
                verdict=row["verdict"],
                response=row.get("response", ""),
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        # Never sleep past the deadline: the user is waiting on this.
        await asyncio.sleep(min(DISSENT_POLL_SECONDS, remaining))

    # Nobody answered in time. Closing the row is a compare-and-set, so a
    # reviewer finishing in this same instant still keeps its verdict recorded.
    if swarm.timeout_challenge(challenge_id):
        swarm.incr_metric("dissent_timeouts")
        log.info("Dissent timeout on '%s': %s did not review in %.0fs", topic, reviewer, timeout)
        return f"{answer}\n\n{UNREVIEWED_MARK}"

    row = swarm.get_challenge(challenge_id) or {}
    if row.get("state") == "answered":
        return format_reviewed_answer(
            answer,
            reviewer=reviewer,
            verdict=row["verdict"],
            response=row.get("response", ""),
        )
    return answer


async def run_challenge_intake(agent, swarm, *, reviewer_agent: str, max_per_poll: int = 3) -> int:
    """Reviewer side: answer pending challenges. Returns how many were handled.

    Called from the council intake pass rather than its own scheduler job — the
    queue shape is identical and a second 30s poller would buy nothing.
    """
    framing = (
        "Коллега-агент просит ревью своего ответа перед отправкой. Найди самое "
        "слабое место: неверную предпосылку, пропущенный риск, ошибку в выводе. "
        "Отвечай КРАТКО (2–3 предложения). Если возражений нет — начни ответ "
        "словом «Согласен»."
    )

    handled = 0
    while handled < max_per_poll:
        challenge = swarm.accept_next_challenge(reviewer_agent)
        if challenge is None:
            break
        handled += 1

        prompt = (
            f"Тема: {challenge['topic'] or 'без темы'}\n"
            f"Автор: {challenge['author_agent']}\n\n"
            f"Черновик ответа:\n{challenge['claim']}"
        )
        try:
            response = await agent.ainvoke(
                message=prompt,
                thread_id=challenge["thread_id"],
                user_id="dissent",
                session_id=str(challenge["chat_id"]),
                # Ephemeral, like a council position: a review is not the user's
                # turn and must not enter this agent's own history.
                source_kind="peer_reaction",
                persist_user_turn=False,
                extra_system_context=framing,
            )
        except Exception as e:
            log.error("Challenge #%s review failed: %s", challenge["id"], e)
            continue

        if not response:
            continue
        swarm.answer_challenge(
            challenge["id"],
            verdict=classify_verdict(response),
            response=response,
        )

    return handled
