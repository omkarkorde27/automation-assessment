"""The discovery loop: observe -> decide -> act, against a live surface.

This is the only place in the system where a model decides anything. Everything
it decides is captured in a `DiscoveryRun`, and the recorder turns that into an
artifact that replays without it.

The loop is written by hand rather than delegated to the SDK's tool runner,
because the loop *is* the product here. It needs a policy choke point before
every action, a budget it cannot talk its way out of, context pruning it does
not control, and a dead-end detector -- none of which is a callback on somebody
else's loop.

Four things terminate a run, and all four are the loop's decision rather than
the model's: the step budget, the wall clock, three identical screens in a row,
and a policy denial. `finish` and `give_up` are the model's two ways of ending
it, and both are recorded as legitimate results.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass

from ..observability.journal import Journal, MemoryJournal
from ..perception.model import Observation, UiNode
from ..policy.redaction import apply_sensitivity, screenshot_masks
from ..policy.risk import IrreversibleActionGuard, classify
from ..profiles.resolve import ResolvedProfile
from ..surfaces.base import Action, ActionType, PolicyDenied, Surface
from . import prompts
from .annotate import annotate
from .model import ModelClient, ModelResponse
from .tools import TOOLS, TOOL_NAMES
from .transcript import DeclaredExtraction, DeclaredOutcome, DiscoveryRun, DiscoveryStep

# Only the most recent observations keep their screenshot. Older turns degrade
# to their text listing: images dominate the context window, and a screenshot
# from eight steps ago has no bearing on the next decision. Deterministic, and
# it does not depend on a beta.
IMAGE_WINDOW = 3


@dataclass
class DiscoveryLimits:
    max_steps: int = 25
    wall_clock_s: float = 300.0
    repeat_screen_limit: int = 3
    """Identical screen hashes in a row before the run is called stuck. Three,
    not two: a legitimate retry after a slow load looks like two."""


class DiscoveryAgent:
    def __init__(
        self,
        surface: Surface,
        client: ModelClient,
        profile: ResolvedProfile,
        *,
        goal: str,
        tenant: str,
        journal: Journal | None = None,
        limits: DiscoveryLimits | None = None,
        allow_irreversible: bool = False,
        model_name: str = "claude-opus-5",
        screenshots: bool = True,
        evidence=None,
    ) -> None:
        self.surface = surface
        self.client = client
        self.profile = profile
        self.journal = journal or MemoryJournal()
        self.limits = limits or DiscoveryLimits()
        self.guard = IrreversibleActionGuard(allow_irreversible=allow_irreversible)
        # Installed on the surface, not consulted here: an agent that checked its
        # own guard before acting would be a second enforcement point, and the
        # one inside act() is the one that holds for every caller.
        surface.add_guard(self.guard)
        self.screenshots = screenshots
        self.evidence = evidence
        """Optional `EvidenceWriter`. Screenshots are saved through it as they
        are taken, so a run that crashes still leaves its evidence behind."""

        self.run = DiscoveryRun(
            run_id=f"disc_{uuid.uuid4().hex[:12]}",
            goal=goal,
            tenant=tenant,
            base_url=profile.base_url,
            model=model_name,
            profile_ref=profile.lineage[-1] if profile.lineage else "",
            profile_hash=profile.hash,
        )

        self._messages: list[dict] = []
        self._image_turns: list[int] = []
        self._last_observation: Observation | None = None
        self._recent_hashes: list[str] = []
        self._step_index = 0

    # ---- public ---------------------------------------------------------

    async def run_discovery(self) -> DiscoveryRun:
        started = time.monotonic()
        self.journal.emit("discovery.started", run_id=self.run.run_id, goal=self.run.goal,
                          tenant=self.run.tenant, model=self.run.model,
                          profile_hash=self.profile.hash)

        await self._open_application()
        observation = await self._observe()
        self._note_hash(observation)
        self._messages.append({
            "role": "user",
            "content": self._observation_content(
                prompts.goal_message(self.run.goal, self.run.base_url, self.run.tenant),
                observation,
                await self._screenshot(observation),
            ),
        })

        while True:
            if self._step_index >= self.limits.max_steps:
                return self._stop("budget_exhausted",
                                  f"reached the {self.limits.max_steps}-step budget")
            if time.monotonic() - started > self.limits.wall_clock_s:
                return self._stop("budget_exhausted",
                                  f"exceeded {self.limits.wall_clock_s:.0f}s of wall clock")

            try:
                response = self.client.send(
                    system=self._system(), messages=self._messages, tools=TOOLS)
            except Exception as exc:
                # The transcript up to this point is still evidence, and often
                # the most useful kind. Losing it to an exception on turn nine
                # would throw away everything the run had established.
                self.journal.emit("llm.transport_error",
                                  error=f"{type(exc).__name__}: {exc}")
                return self._stop("aborted", f"the model call failed: "
                                             f"{type(exc).__name__}: {exc}")
            self.run.input_tokens += response.input_tokens
            self.run.output_tokens += response.output_tokens

            # stop_reason BEFORE content, every turn. A refusal has no tool calls
            # to parse and a truncated turn may have half of one; treating either
            # as a normal turn is how a loop starts doing something incoherent.
            if terminal := self._terminal_stop_reason(response):
                return terminal

            if not response.tool_calls:
                self.journal.emit("llm.no_action", stop_reason=response.stop_reason,
                                  text=(response.text or "")[:500])
                return self._stop("aborted", "the model ended its turn without acting")

            self._messages.append({"role": "assistant", "content": response.content})

            results, finished = await self._run_tools(response.tool_calls)
            if finished is not None:
                return finished

            self._messages.append({"role": "user", "content": results})
            self._prune_images()

    # ---- the turn -------------------------------------------------------

    def _terminal_stop_reason(self, response: ModelResponse) -> DiscoveryRun | None:
        if response.stop_reason == "refusal":
            return self._stop("aborted", "the model declined to continue (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            return self._stop("aborted",
                              "the model's turn was truncated (stop_reason=max_tokens); "
                              "not retried blind, because half a decision is not a decision")
        return None

    async def _run_tools(self, calls) -> tuple[list[dict], DiscoveryRun | None]:
        """Execute the model's tool calls. Returns (tool_results, terminal_run)."""
        results: list[dict] = []

        for call in calls:
            self._step_index += 1
            reason = str(call.arguments.get("reason", ""))

            # R-M4-2: every tool call the model makes is journalled here, before
            # anything decides what to do with it. Emitting per-branch means the
            # branch nobody thought about is the one that goes unrecorded, and an
            # evidence pack that silently omits a decision is not evidence.
            self.journal.emit(
                "llm.decision", step=self._step_index, tool=call.name, reason=reason,
                args={k: v for k, v in call.arguments.items() if k != "reason"},
            )

            if call.name not in TOOL_NAMES:
                self._record(DiscoveryStep(
                    index=self._step_index, tool=call.name, reason=reason, ok=False,
                    error=f"unknown tool {call.name!r}", failure_class="INTERNAL",
                    pruned=True, pruned_because="not a tool this agent offers"))
                self.journal.emit("llm.unknown_tool", step=self._step_index, tool=call.name)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": f"There is no tool called {call.name!r}."})
                continue

            if call.name == "finish":
                # Recorded for the transcript, pruned from the flow: "the goal is
                # reached" is a statement about the run, not a step to replay.
                self._record(DiscoveryStep(
                    index=self._step_index, tool="finish", reason=reason,
                    pruned=True, pruned_because="a declaration, not an action"))
                self.run.finish_reason = reason
                return results, self._stop("finished", reason)

            if call.name == "give_up":
                self._record(DiscoveryStep(
                    index=self._step_index, tool="give_up", reason=reason,
                    pruned=True, pruned_because="a declaration, not an action"))
                return results, self._stop("gave_up", reason)

            content, denied = await self._dispatch(call, reason)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})

            if denied:
                return results, self._stop("denied", denied)

            if self._is_stuck():
                return results, self._stop(
                    "stuck",
                    f"the screen did not change across {self.limits.repeat_screen_limit} "
                    f"consecutive actions",
                )

        return results, None

    async def _dispatch(self, call, reason: str) -> tuple[list[dict] | str, str]:
        """Perform one tool call. Returns (tool_result content, denial message)."""
        args = call.arguments
        name = call.name
        pre = self._last_observation

        if name == "observe":
            observation = await self._observe()
            self._note_hash(observation)
            self._record(DiscoveryStep(index=self._step_index, tool="observe", reason=reason,
                                       pre=pre, post=observation, pruned=True,
                                       pruned_because="an observation is not a step"))
            return self._observation_content("", observation, await self._screenshot(observation)), ""

        if name == "extract":
            node = self._node(args.get("node_id", ""))
            if node is None:
                return self._bad_node(args.get("node_id", ""), "extract", reason), ""
            self.run.extractions.append(DeclaredExtraction(
                output_name=args.get("output_name", "value"), node=node,
                at_step=self._step_index, reason=reason))
            self._record(DiscoveryStep(index=self._step_index, tool="extract", reason=reason,
                                       node=node, pre=pre, post=pre, pruned=True,
                                       pruned_because="recorded as an extraction, not a step"))
            self.journal.emit("discovery.extraction", output=args.get("output_name"),
                              node=node.describe(), reason=reason)
            return f"Recorded '{args.get('output_name')}' from {node.describe()}.", ""

        if name == "declare_outcome":
            node = self._node(args.get("node_id", "")) if args.get("node_id") else None
            self.run.outcomes.append(DeclaredOutcome(
                code=args.get("code", "UNKNOWN"), hint=args.get("hint", ""),
                at_step=self._step_index, observed_node=node))
            self._record(DiscoveryStep(index=self._step_index, tool="declare_outcome",
                                       reason=reason, node=node, pre=pre, post=pre,
                                       pruned=True,
                                       pruned_because="recorded as an outcome, not a step"))
            self.journal.emit("discovery.outcome_declared", code=args.get("code"), reason=reason)
            return f"Declared business outcome {args.get('code')}.", ""

        return await self._act(call, reason)

    async def _act(self, call, reason: str) -> tuple[list[dict] | str, str]:
        """Tools that touch the surface."""
        args, name = call.arguments, call.name
        pre = self._last_observation
        node: UiNode | None = None
        value = None
        param_name = ""
        is_param = False

        if name in ("click", "fill", "select_option"):
            node = self._node(args.get("node_id", ""))
            if node is None:
                return self._bad_node(args.get("node_id", ""), name, reason), ""

        if name == "click":
            action_type = ActionType.CLICK
        elif name == "fill":
            action_type = ActionType.FILL
            value = str(args.get("text", ""))
            is_param = bool(args.get("is_param_candidate"))
            param_name = str(args.get("param_name", "") or "")
        elif name == "select_option":
            action_type = ActionType.SELECT_OPTION
            value = str(args.get("value", ""))
            is_param = bool(args.get("is_param_candidate"))
            param_name = str(args.get("param_name", "") or "")
        elif name == "press_key":
            action_type = ActionType.PRESS_KEY
        elif name == "navigate":
            action_type = ActionType.NAVIGATE
        elif name == "scroll":
            action_type = ActionType.SCROLL
        elif name == "wait_for_text":
            return await self._wait_for_text(str(args.get("text", "")), reason), ""
        else:  # pragma: no cover - unknown tools are rejected before dispatch
            return f"Unknown tool {name}.", ""

        risk = classify(action_type, node)
        action = Action(
            type=action_type,
            target=self._bundle_for(node) if node is not None else None,
            value=value,
            # `str(args.get(...))` turned a missing field into the literal
            # string "None" -- a navigate to "None", a keypress of "None".
            # Strict schemas should prevent it; relying on that is how a silent
            # coercion survives.
            url=(str(args["url"]) if args.get("url") is not None else None)
            if name == "navigate" else None,
            key=(str(args["key"]) if args.get("key") is not None else None)
            if name == "press_key" else None,
            risk=risk,
            reason=reason,
        )

        self.journal.emit("llm.target_resolved", step=self._step_index, tool=name,
                          target=node.describe() if node else action.url or "",
                          risk=risk.value)

        try:
            result = await self.surface.act(action)
        except PolicyDenied as exc:
            result = None
            self._record(DiscoveryStep(
                index=self._step_index, tool=name, reason=reason, action_type=action_type,
                node=node, value=value, risk=risk, ok=False, error=exc.message,
                failure_class="POLICY_DENIED", pre=pre, post=pre, pruned=True,
                pruned_because="refused by policy"))
            self.journal.emit("policy.denied", step=self._step_index, reason=exc.message)
            return f"REFUSED: {exc.message}", exc.message

        observation = await self._observe_settled(action_type)
        self._note_hash(observation, action_type)

        denial = ""
        if not result.ok and result.error_detail.get("failure_class") == "POLICY_DENIED":
            denial = result.error or "policy denied"
            self.journal.emit("policy.denied", step=self._step_index, reason=denial)

        step = DiscoveryStep(
            index=self._step_index, tool=name, reason=reason, action_type=action_type,
            node=node, value=value, is_param_candidate=is_param, param_name=param_name,
            url=action.url, risk=risk, ok=result.ok, error=result.error or "",
            failure_class=result.error_detail.get("failure_class", ""),
            pre=pre, post=observation,
        )
        if not result.ok:
            step.pruned = True
            step.pruned_because = f"the action failed: {result.error}"
        self._record(step)

        self.journal.emit("action.executed", step=self._step_index, action=action_type.value,
                          ok=result.ok, error=result.error,
                          resolved=result.resolved.describe() if result.resolved else "")

        header = "" if result.ok else f"That action failed: {result.error}\n\n"
        return self._observation_content(header, observation,
                                         await self._screenshot(observation)), denial

    async def _wait_for_text(self, text: str, reason: str) -> list[dict] | str:
        needle = text.casefold()
        deadline = time.monotonic() + 10
        observation = await self._observe()
        while time.monotonic() < deadline:
            if any(needle in (n.name or "").casefold() for n in observation.nodes):
                break
            await self.surface.act(Action(ActionType.WAIT, timeout_ms=500, reason="poll"))
            observation = await self._observe()

        self._note_hash(observation)
        self._record(DiscoveryStep(index=self._step_index, tool="wait_for_text", reason=reason,
                                   pre=self._last_observation, post=observation,
                                   pruned=True,
                                   pruned_because="waiting is a recovery, not a recorded step"))
        return self._observation_content("", observation, await self._screenshot(observation))

    # ---- perception -----------------------------------------------------

    async def _observe(self) -> Observation:
        raw = await self.surface.observe()
        # Redaction happens the moment an observation enters the agent, so there
        # is no window in which an unredacted one could be attached to a message.
        observation = apply_sensitivity(raw, self.profile.profile)
        self._last_observation = observation
        self.journal.emit("observation.captured", step=self._step_index,
                          nodes=len(observation.nodes), url=observation.url,
                          state=observation.state_hash())
        return observation

    async def _observe_settled(self, action_type: ActionType) -> Observation:
        """Observe once the screen has stopped moving.

        A click returns before the page it triggered has loaded, so observing
        immediately captures the screen the model was already looking at. That
        is worse than a slow loop: the model is shown a stale screen and told it
        is the result of its action, and the dead-end detector counts a working
        run as making no progress.

        Only actions that can change the screen are waited on. Typing into a
        field changes the field's value, not the set of things on screen, so
        waiting for a change after a `fill` would always burn the full timeout.
        """
        observation = await self._observe()
        if action_type not in (ActionType.CLICK, ActionType.PRESS_KEY, ActionType.NAVIGATE):
            return observation

        before = self._recent_hashes[-1] if self._recent_hashes else ""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if observation.state_hash() != before and self._frames_present(observation):
                break
            await self.surface.act(Action(ActionType.WAIT, timeout_ms=250,
                                          reason="let the screen settle before observing"))
            observation = await self._observe()
        return observation

    def _frames_present(self, observation: Observation) -> bool:
        """Are the frames this application declares it has actually there?

        A frame that is mid-navigation yields no nodes, and an observation
        missing its content frame has a *different* state hash -- so "the hash
        changed" on its own reports a half-loaded screen as the settled result.
        The model is then shown a page consisting of the navigation menu and
        nothing else, and concludes the control it wanted is gone.

        The profile already declares the topology, which is exactly so that a
        missing frame is a recognisable condition rather than a silent one.
        """
        required = {f.name for f in self.profile.profile.surface.frame_topology if f.required}
        if not required:
            return True
        present = {"/".join(path) for path in observation.frame_paths}
        return required <= present

    def _note_hash(self, observation: Observation,
                   action_type: ActionType | None = None) -> None:
        """Record one screen state per *step*, for the dead-end detector.

        Two things are deliberately excluded.

        The settle loop above observes repeatedly; counting each poll would make
        every navigation look like a dead end.

        And typing does not change the screen *by construction*: the state hash
        is over roles and names, not values, so filling a field leaves it
        identical. Counting input actions means any form with three fields
        reports a model that is filling it in correctly as making no progress --
        which is exactly what happened on the first real run of the
        sub-account flow. The step budget is what bounds a model that only ever
        types; this detector is for one that clicks a dead control forever.
        """
        if action_type in (ActionType.FILL, ActionType.SELECT_OPTION):
            return
        self._recent_hashes.append(observation.state_hash())

    async def _screenshot(self, observation: Observation) -> bytes | None:
        if not self.screenshots or not self.surface.capabilities.can_screenshot:
            return None
        try:
            png = await self.surface.screenshot()
        except Exception:
            return None
        annotated = annotate(png, observation,
                             masks=screenshot_masks(observation, self.profile.profile))

        # Written here, where the image is already annotated AND masked -- the
        # evidence pack must never hold a picture the model was not allowed to
        # see. Part 7 asks for every step of a discovery run.
        if self.evidence is not None:
            try:
                self.evidence.screenshot(self._step_index, annotated)
            except Exception:
                pass
        return annotated

    def _observation_content(self, header: str, observation: Observation,
                             png: bytes | None) -> list[dict]:
        text = header + prompts.render_observation(observation)
        content: list[dict] = [{"type": "text", "text": text}]
        if png:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.b64encode(png).decode()},
            })
            self._image_turns.append(len(self._messages))
        return content

    def _prune_images(self) -> None:
        """Drop all but the last few screenshots from the conversation.

        Images are by far the largest thing in this context window, and an
        observation from ten steps ago cannot inform the next click. The text
        listing stays, so nothing is forgotten -- only the picture of it.
        """
        keep = set(self._image_turns[-IMAGE_WINDOW:])
        for turn in self._image_turns[:-IMAGE_WINDOW]:
            if turn in keep or turn >= len(self._messages):
                continue
            message = self._messages[turn]
            content = message.get("content")
            if not isinstance(content, list):
                continue
            message["content"] = [
                {"type": "text", "text": "[screenshot dropped to save context]"}
                if part.get("type") == "image" else part
                for part in content
            ]
        self._image_turns = self._image_turns[-IMAGE_WINDOW:]

    # ---- helpers --------------------------------------------------------

    def _system(self) -> list[dict]:
        # Cached: the system prompt and the tool list are identical across every
        # turn of every run against this application.
        return [{"type": "text", "text": prompts.SYSTEM,
                 "cache_control": {"type": "ephemeral"}}]

    def _node(self, node_id: str) -> UiNode | None:
        if self._last_observation is None:
            return None
        return self._last_observation.by_id(node_id)

    def _bad_node(self, node_id: str, tool: str = "", reason: str = "") -> str:
        """A tool call naming an element that is not on screen.

        Recorded as a pruned step rather than silently answered. A model that
        invents node ids leaves gaps in the step numbering otherwise, and the
        evidence pack should show the mistakes as well as the successes -- the
        gaps are how you find out a weaker model was hallucinating ids rather
        than reading the screen.
        """
        self._record(DiscoveryStep(
            index=self._step_index, tool=tool or "?", reason=reason, ok=False,
            error=f"no element {node_id!r} on screen", failure_class="LOCATOR_UNRESOLVED",
            pre=self._last_observation, post=self._last_observation,
            pruned=True, pruned_because="referenced an element that was not on screen"))
        return (f"There is no element {node_id!r} on the current screen. "
                f"Observe again and use a node_id exactly as listed.")

    def _bundle_for(self, node: UiNode):
        """A minimal bundle so `act()` can re-resolve the node it was given.

        Deliberately not the recorded bundle: the recorder derives that from the
        same node afterwards, with the full candidate ladder and stability
        scoring. This one only has to survive the microseconds between choosing
        the node and clicking it.
        """
        from ..locators.model import FrameRef, LocatorBundle, RoleNameExact, RoleOrdinal
        from ..locators.model import Anchor, SectionScope

        frame_path = tuple(FrameRef(name=part) for part in node.frame_path)
        candidates: list = []
        if node.name:
            candidates.append(RoleNameExact(role=node.role, name=node.name))
        if node.anchors.row_label:
            from ..locators.model import AnchorRelative
            candidates.append(AnchorRelative(
                anchor=Anchor(text=node.anchors.row_label), relation="same_row",
                target_role=node.role))
        if not candidates:
            pool = [n for n in (self._last_observation.nodes if self._last_observation else ())
                    if n.role == node.role and n.frame_path == node.frame_path]
            candidates.append(RoleOrdinal(
                role=node.role, ordinal=pool.index(node) if node in pool else 0,
                scope=SectionScope(
                    anchor=Anchor(text=node.anchors.section_label or node.anchors.nearest_heading
                                  or "document"),
                    relation="within_section")))

        return LocatorBundle(
            target_id=f"live_{node.node_id.replace(':', '_')}",
            frame_path=frame_path, candidates=tuple(candidates),
            notes="Live selection during discovery; the recorder derives the durable bundle.",
        )

    def _record(self, step: DiscoveryStep) -> None:
        self.run.steps.append(step)

    def _is_stuck(self) -> bool:
        limit = self.limits.repeat_screen_limit
        if len(self._recent_hashes) < limit:
            return False
        recent = self._recent_hashes[-limit:]
        return len(set(recent)) == 1

    def _stop(self, status: str, reason: str) -> DiscoveryRun:
        from datetime import datetime, timezone

        self.run.status = status
        self.run.stop_reason = reason
        self.run.ended_at = datetime.now(timezone.utc)
        self.journal.emit("discovery.finished", status=status, reason=reason,
                          steps=len(self.run.steps), kept=len(self.run.kept_steps),
                          input_tokens=self.run.input_tokens,
                          output_tokens=self.run.output_tokens)
        return self.run

    async def _open_application(self) -> None:
        if not self.surface.capabilities.can_navigate:
            return
        current = await self.surface.current_url()
        if current in ("", "about:blank"):
            await self.surface.act(Action(
                ActionType.NAVIGATE, url=f"{self.profile.base_url}/",
                reason="open the application before discovery begins"))
