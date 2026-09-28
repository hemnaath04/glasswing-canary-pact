"use client";

import { useEffect, useMemo, useState } from "react";
import type { DecisionPackage, Event } from "@canary-pact/contracts/generated";
import { REVIEW_STAGES, decisionHighlights, reviewSnapshot } from "@/lib/review-story";
import { formatCompactCurrency } from "@/lib/formatters";

const name = (value: string) => value.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
const descriptions = [
  "Start with the proposed change, its objective, and the constraints it must respect.",
  "Departments assess the same proposal independently. These are their initial positions.",
  "Specific objections test the assumptions behind those positions.",
  "Affected departments answer linked objections once. A reply records a position, not consensus.",
  "Validated dependencies feed the engine. These values come from the recorded calculations.",
  "The trade-off, the main risks, and what to check before you commit.",
];

export function ReviewStory({events, complete, decisionPackage, onEvidence, embedded = false, selectedStage, onStageChange}: {
  events: Event[]; complete: boolean; decisionPackage?: DecisionPackage | null; onEvidence?: (id: string) => void;
  embedded?: boolean; selectedStage?: number | null; onStageChange?: (stage: number | null) => void;
}) {
  const snapshot = useMemo(() => reviewSnapshot(events, decisionPackage), [events, decisionPackage]);
  const [selected, setSelected] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const stage = (onStageChange ? selectedStage : selected) ?? snapshot.stage;
  const selectStage = onStageChange ?? setSelected;
  useEffect(() => {
    if (!playing) return;
    const timer = setTimeout(() => {
      const next = stage + 1;
      if (next >= 5) setPlaying(false);
      selectStage(Math.min(next, 5));
    }, 6000);
    return () => clearTimeout(timer);
  }, [playing, stage, selectStage]);
  const choose = (index: number) => {setPlaying(false); selectStage(index);};
  const sources = (refs: string[] = []) => refs.length > 0 ? <div className="mt-3 flex flex-wrap gap-2">{refs.map(ref =>
    onEvidence ? <button key={ref} onClick={() => onEvidence(ref)} className="break-all rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs text-emerald-900">Source · {ref}</button>
      : <span key={ref} className="break-all text-xs text-zinc-500">Source · {ref}</span>)}</div> : null;
  const first = snapshot.assessments.filter(a => a.pass_type === "first_pass");
  const challenges = snapshot.assessments.filter(a => a.pass_type === "challenge" || (a.pass_type === "first_pass" && a.status === "ok" && a.output?.objections?.length));
  const pkg = snapshot.decisionPackage;
  const recommendation = pkg?.recommendation;
  const selectedRow = pkg?.futures.rows.find(row => row.result_id === recommendation?.result_id);
  // A mitigated recommendation is judged on the mitigated plan, not the base row it was derived from.
  const mitigated = recommendation?.mitigated_result_id ? pkg?.mitigations?.find(item => item.after.result_id === recommendation.mitigated_result_id)?.after : undefined;
  const outcome = mitigated ? {feasible: mitigated.feasible, net: mitigated.value.p50_net_value_usd ?? mitigated.value.net_value_usd} : selectedRow ? {feasible: selectedRow.feasible, net: selectedRow.net_value_p50_usd} : undefined;
  const highlights = pkg ? decisionHighlights(snapshot.assessments, pkg) : null;
  const needsReview = Boolean(pkg?.open_questions?.length || pkg?.missing_perspectives?.length || pkg?.missing_information?.length || highlights?.risks.length || pkg?.critical_risks?.length);
  const recommendationLabel = recommendation?.action === "do_not_proceed" ? "Do not proceed" : recommendation?.action === "delay" ? "Delay" : recommendation?.action === "proceed_with_mitigations" ? "Proceed with mitigations" : recommendation?.future === "inaction" ? "Keep the current plan unchanged" : recommendation?.future === "alternative" ? "Consider the alternative plan" : recommendation?.future === "delay" ? "Delay the change" : "Act now";
  const empty = (text: string) => <p role="status" className="rounded-xl border border-dashed border-zinc-300 bg-zinc-50 p-5 text-sm text-zinc-600">{text}</p>;
  return <section className={embedded ? "board-story flex min-h-0 flex-1 flex-col" : "flex min-h-0 flex-1 flex-col rounded-2xl border border-zinc-200 bg-white shadow-sm"} aria-label="Decision story">
    {!embedded ? <nav className="grid grid-cols-3 gap-1 border-b p-2 lg:grid-cols-6" aria-label="Review stages">
      {REVIEW_STAGES.map((label, index) => <button key={label} onClick={() => choose(index)} aria-current={stage === index ? "step" : undefined}
        className={`flex items-center gap-2 rounded-lg px-2 py-3 text-left text-xs font-medium ${stage === index ? "bg-emerald-950 text-white" : "text-zinc-600 hover:bg-zinc-100"}`}>
        <span className={`grid size-5 shrink-0 place-items-center rounded-full text-[10px] ${stage === index ? "bg-white/20" : "bg-zinc-100"}`}>{index + 1}</span>{label}
      </button>)}
    </nav> : null}
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className={stage === 5 ? "sr-only" : "mb-5"}><p className="text-[10px] font-semibold uppercase tracking-widest text-emerald-700">{complete ? "Recorded review" : "Live review"} · Stage {stage + 1} of 6</p>
        <h2 className="mt-1 text-2xl font-semibold tracking-tight">{REVIEW_STAGES[stage]}</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-500">{descriptions[stage]}</p></div>
      {snapshot.failure ? <p role="alert" className="mb-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">Run failed: {snapshot.failure}. Received findings remain available.</p> : null}
      <div className="space-y-4 text-sm leading-6">
      {stage === 0 && (snapshot.brief ? <>
        <h3 className="text-xl font-medium">{snapshot.brief.title}</h3><p>{snapshot.brief.statement}</p>
        <p className="text-zinc-500">{snapshot.brief.horizon_days} days · {snapshot.brief.candidate_interventions.length} proposed interventions · {snapshot.brief.constraints?.length ?? 0} constraints</p>
        <details><summary className="cursor-pointer font-medium">Proposed interventions and constraints</summary><ul className="mt-3 space-y-2">{snapshot.brief.candidate_interventions.map(i => <li key={i.id}>{name(i.type)} · {name(i.target_entity_id)}{i.rationale ? ` — ${i.rationale}` : ""}</li>)}{snapshot.brief.constraints?.map(c => <li key={c.id}>{c.description}</li>)}</ul></details>
      </> : empty("Waiting for the decision brief."))}
      {stage === 1 && (first.length ? first.map(a => <article key={a.assessment_id} className="rounded-xl border p-4">
        <h3 className="font-semibold">{name(a.agent_id)} <span className="ml-2 text-xs font-normal text-zinc-500">Initial assessment</span></h3>
        {a.status === "ok" && a.output ? <><p className="mt-2"><strong>Act now: </strong>{a.output.act_now_view.summary}</p><p className="mt-2 text-zinc-600"><strong>Do not act: </strong>{a.output.inaction_view.summary}</p>{sources(a.output.evidence_refs)}<p className="mt-2 text-xs text-zinc-500">Confidence {Math.round(a.output.confidence * 100)}%</p></>
          : <p className="mt-2 text-amber-800">Assessment unavailable. {a.validation?.errors?.join("; ")}</p>}
      </article>) : empty(complete ? "No department assessments were recorded." : "Departments are analyzing the proposal. Findings will appear here."))}
      {stage === 2 && (challenges.length ? challenges.map(a => <details key={a.assessment_id} open={a.pass_type === "challenge"} className="rounded-xl border border-amber-200 bg-amber-50/40 p-4">
        <summary className="cursor-pointer font-semibold">{name(a.agent_id)} · {a.status === "ok" ? "Review concerns" : "Review unavailable"}</summary>
        {a.status !== "ok" ? <p className="mt-2 text-amber-800">No valid challenge assessment was received. This perspective remains missing.</p> : null}
        {(a.challenge?.objections ?? a.output?.objections ?? []).map((o, i) => <div key={i} className="mt-3"><p className="text-xs font-medium text-amber-800">To {name(first.find(f => f.assessment_id === o.target_ref)?.agent_id ?? o.target_ref)} · Severity {o.severity}</p><p>{o.text}</p></div>)}
        {[...(a.challenge?.unsupported_assumptions ?? []), ...(a.challenge?.circular_logic ?? []), ...(a.challenge?.inaction_underestimated ?? [])].map((f,i) => <div key={i} className="mt-3"><p>{f.text}</p>{sources(f.evidence_refs)}</div>)}
        {(a.challenge?.missed_dependencies ?? []).map((d,i) => <div key={i} className="mt-3"><p className="font-medium">Missing dependency · {name(d.source)} → {name(d.target)}</p><p>{d.rationale}</p>{sources(d.evidence_refs)}</div>)}
        {a.challenge && !a.challenge.objections?.length && !a.challenge.unsupported_assumptions?.length && !a.challenge.circular_logic?.length && !a.challenge.inaction_underestimated?.length && !a.challenge.missed_dependencies?.length ? <p className="mt-2 text-zinc-500">No specific objections were supplied.</p> : null}
      </details>) : empty(complete ? "No objections were recorded. No exchange was invented." : "Waiting for the challenge review."))}
      {stage === 3 && (snapshot.issues.length ? snapshot.issues.map(issue => {
        const original = first.find(a => a.assessment_id === issue.target_assessment_id);
        const response = snapshot.assessments.find(a => a.pass_type === "response" && a.review_issues?.some(i => i.issue_id === issue.issue_id));
        const reply = response?.status === "ok" ? response.output?.review_replies?.find(r => r.issue_id === issue.issue_id) : undefined;
        return <article key={issue.issue_id} className="overflow-hidden rounded-xl border">
          <div className="bg-zinc-50 p-4"><p className="text-xs font-semibold text-zinc-500">{name(original?.agent_id ?? "department")} · Original position</p><p className="mt-1"><strong>Act now: </strong>{original?.output?.act_now_view.summary ?? "Original assessment unavailable."}</p><p className="mt-2 text-zinc-600"><strong>Do not act: </strong>{original?.output?.inaction_view.summary ?? "Original assessment unavailable."}</p></div>
          <div className="border-t border-amber-100 bg-amber-50/50 p-4"><p className="text-xs font-semibold text-amber-800">{name(issue.source_agent_id)} → {name(original?.agent_id ?? "department")}</p><p className="mt-1">{issue.text}</p>{sources(issue.evidence_refs)}</div>
          <div className="border-t p-4"><p className="text-xs font-semibold text-emerald-800">{name(original?.agent_id ?? "department")} · {reply ? name(reply.position) : response || complete ? "Unresolved" : "Responding"}</p>
            <p className="mt-1">{reply?.explanation ?? (response || complete ? "No valid response was received. The objection remains open." : "Waiting for the department’s response.")}</p>{sources(reply?.evidence_refs)}
            {reply?.position === "revised" && response?.output ? <div className="mt-3 space-y-2 text-zinc-600"><p><strong>Updated act-now view: </strong>{response.output.act_now_view.summary}</p><p><strong>Updated do-not-act view: </strong>{response.output.inaction_view.summary}</p></div> : null}
          </div></article>;
      }) : empty(complete ? "No targeted response round was recorded for this run. Any remaining concerns stay open for human review." : "Specific objections will be routed to affected departments. At most three departments respond once."))}
      {stage === 4 && <>
        {snapshot.latest ? <><div className="grid gap-3 sm:grid-cols-2">{[{label:"Before department review", result:snapshot.initial}, {label:"Latest engine calculation · Act now", result:snapshot.latest}].map(({label,result}) => <article key={label} className="rounded-xl border p-4"><h3 className="text-xs font-medium text-zinc-500">{label}</h3>{result ? <><p className="mt-2 text-2xl font-semibold">{formatCompactCurrency(result.value.net_value_usd)}</p><p>Net value · {result.feasible ? "Within modeled constraints" : "Not feasible"}</p><p className="mt-1 break-all text-xs text-zinc-500">{result.plan_id}</p></> : <p>Not recorded.</p>}</article>)}</div>
          <p className="text-xs text-zinc-500">Agent replies do not change calculated values directly. {snapshot.initial ? snapshot.initial.plan_id !== snapshot.latest.plan_id ? "The engine selected a different plan after review." : "Both calculations refer to the same plan." : "The initial calculation is unavailable for comparison."}</p>
          {snapshot.dependencies.length ? <div><h3 className="font-semibold">Dependencies accepted by validation</h3>{snapshot.dependencies.map((d,i) => <div key={i} className="mt-2 rounded-lg bg-emerald-50 p-3"><p>{name(d.source)} → {name(d.target)}</p>{sources(d.evidence)}</div>)}</div> : <p>No new validated dependencies were recorded. Discussion alone does not establish a change in outcome.</p>}
          <details><summary className="cursor-pointer font-semibold">When effects appear · {snapshot.latest.impacts?.length ?? 0} modeled impacts</summary><div className="mt-3 space-y-3">{[...(snapshot.latest.impacts ?? [])].sort((a,b) => a.first_effect_day - b.first_effect_day).map(impact => <article key={impact.impact_id} className="rounded-xl border p-4"><p className="text-xs font-semibold text-zinc-500">First effect: day {impact.first_effect_day} · Peak: day {impact.peak_effect_day}</p><h4 className="mt-1 font-medium">{name(impact.metric)} · {name(impact.affected_entity)}</h4><p>{impact.magnitude} {impact.unit} · {impact.direction} · {impact.polarity}</p><p className="mt-1 text-zinc-500">{impact.dependency_path?.length ? impact.dependency_path.map(name).join(" → ") : "No dependency path supplied."}</p><p className="text-xs text-zinc-500">Confidence {Math.round(impact.confidence * 100)}% · Severity {impact.severity}</p>{sources(impact.evidence_refs)}</article>)}</div></details>
        </> : empty("No act-now calculations are available in the event history.")}
      </>}
      {stage === 5 && (pkg ? <>
        <article className="rounded-xl border border-amber-200 bg-amber-50 p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-amber-800">Your next step</p>
          <h3 className="mt-1 text-xl font-semibold">{!recommendation || !outcome ? "Request a complete analysis" : !outcome.feasible ? "Rework the plan before approval" : needsReview ? "Review the risks before acting" : "Review the recommendation for approval"}</h3>
          <p className="mt-1 text-sm text-zinc-700">{needsReview ? "Resolve the checks below or request a revised scenario; savings alone do not establish safety." : "Confirm the assumptions and operational readiness. A human makes the final decision."}</p>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-amber-200 pt-3 text-xs">
            <span>Model recommendation: <strong>{recommendation ? recommendationLabel : "Unavailable"}</strong></span>
            {outcome ? <span><strong>{formatCompactCurrency(outcome.net)}</strong> modeled net value over {pkg.brief.horizon_days} days{mitigated ? " with mitigations" : ""} · {outcome.feasible ? "Passes modeled constraints" : "Fails modeled constraints"}</span> : null}
          </div>
          <p className="mt-1 text-xs text-zinc-500">Point estimate; unmeasured effects may change the outcome.</p>
        </article>
        <div>
          <h3 className="font-semibold">What could go wrong</h3>
          <p className="text-xs text-zinc-500">Department concerns about the proposed change. These are not quantified predictions.</p>
          {highlights?.risks.length ? <div className="mt-2 grid items-start gap-2 lg:grid-cols-3">{highlights.risks.map(({assessment, finding}) => <details key={assessment.assessment_id} className="group rounded-xl border p-3">
            <summary className="cursor-pointer list-none"><span className="text-xs font-semibold text-amber-800">{name(assessment.agent_id)}</span><p className="mt-1 line-clamp-3 text-sm leading-5 group-open:hidden">{finding.text}</p><span className="mt-2 block text-xs text-emerald-800 group-open:hidden">Read concern & evidence →</span></summary>
            <p className="mt-2 text-sm leading-5">{finding.text}</p>
            <p className="mt-2 text-xs text-zinc-500">{assessment.pass_type === "response" ? "After challenge" : "Initial assessment"} · Confidence {Math.round((assessment.output?.confidence ?? 0) * 100)}%</p>
            {sources(finding.evidence_refs)}{!finding.evidence_refs?.length ? <p className="mt-2 text-xs text-amber-800">No source attached to this concern.</p> : null}
          </details>)}</div> : <p className="mt-2 text-sm text-zinc-500">No department risk summaries are available. Missing findings do not establish that there is no risk.</p>}
          {pkg.critical_risks?.length ? <details className="mt-2 rounded-xl border p-3"><summary className="cursor-pointer text-sm font-medium">Engine-reported critical risks · {pkg.critical_risks.length}</summary>{pkg.critical_risks.map(impact => <div key={impact.impact_id} className="mt-3"><p>{name(impact.metric)} · {name(impact.affected_entity)} · First effect day {impact.first_effect_day}</p><p className="text-xs">{impact.magnitude} {impact.unit} · {impact.direction} · {impact.polarity}</p>{sources(impact.evidence_refs)}</div>)}</details> : null}
        </div>
        {pkg.mitigations?.length ? <div>
          <h3 className="font-semibold">Mitigations</h3>
          <p className="text-xs text-zinc-500">Before and after results from engine re-simulation.</p>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">{pkg.mitigations.map(mitigation => <article key={mitigation.after.result_id} className="rounded-xl border p-4">
            <h4 className="break-words font-medium">{name(mitigation.plan_id_before)} → {name(mitigation.plan_id_after)}</h4>
            <dl className="mt-3 grid grid-cols-2 gap-3">
              <div><dt className="text-xs text-zinc-500">Risk before</dt><dd>{mitigation.before.risk.score} · {name(mitigation.before.risk.level)}</dd></div>
              <div><dt className="text-xs text-zinc-500">Risk after</dt><dd>{mitigation.after.risk.score} · {name(mitigation.after.risk.level)}</dd></div>
            </dl>
            <p className="mt-3 text-xs font-medium text-zinc-500">Restored entities</p>
            {mitigation.restored_entity_ids?.length ? <ul className="mt-1 space-y-1">{mitigation.restored_entity_ids.map(id => <li key={id} className="break-words">{name(id)}</li>)}</ul> : <p className="mt-1 text-zinc-500">No restored entities reported.</p>}
          </article>)}</div>
        </div> : null}
        {highlights?.questions.length ? <div><h3 className="font-semibold">Before you decide</h3><ol className="mt-2 grid items-start gap-2 lg:grid-cols-3">{highlights.questions.map((question, i) => <li key={i} className="rounded-lg bg-zinc-50 p-3"><details className="group"><summary className="cursor-pointer list-none"><span className="text-xs font-medium text-zinc-500">Check {i + 1} · Expand</span><span className="mt-1 line-clamp-3 text-sm leading-5 group-open:hidden">{question}</span></summary><p className="mt-1 text-sm leading-5">{question}</p></details></li>)}</ol></div> : null}
        <details className="rounded-xl border p-3"><summary className="cursor-pointer font-semibold">Full review & calculation · {pkg.open_questions?.length ?? 0} open concerns</summary>
          <p className="mt-3">{recommendation?.headline ?? "No recommendation was produced."}</p>
          {pkg.assumptions?.length ? <details className="mt-3"><summary className="cursor-pointer font-medium">Calculation assumptions</summary><ul className="mt-2 space-y-2">{pkg.assumptions.map((text,i) => <li key={i}>{text}</li>)}</ul></details> : null}
          {pkg.open_questions?.length ? <ul className="mt-3 space-y-2">{pkg.open_questions.map((q,i) => <li key={i} className="rounded-lg bg-amber-50 p-3">{q}</li>)}</ul> : <p className="mt-2">No open concerns were supplied. This does not establish certainty.</p>}
        </details>
        {pkg.missing_perspectives?.length ? <p className="text-amber-800">Missing perspectives: {pkg.missing_perspectives.map(name).join(", ")}</p> : null}
        <p className="text-xs text-zinc-500">{snapshot.humanDecision ? `Human decision recorded: ${name(snapshot.humanDecision.decision)}. ${snapshot.humanDecision.notes || "No organizational changes were executed."}` : "Human approval required. This review does not execute organizational changes."}</p>
      </> : empty(complete ? "This run has no final decision package." : "The engine is preparing the decision package."))}
      </div>
    </div>
    <footer className="flex flex-wrap items-center justify-between gap-2 border-t p-3 text-xs">
      <button className="rounded-lg border px-3 py-2 disabled:opacity-40" disabled={stage === 0} onClick={() => choose(stage - 1)}>← Previous</button>
      {complete ? <button className="rounded-lg px-3 py-2 text-zinc-600" onClick={() => {if (!playing) selectStage(stage >= 5 ? 0 : stage); setPlaying(!playing);}}>{playing ? "Pause review" : "Play stages"}</button> : <button className="rounded-lg px-3 py-2 text-emerald-700" onClick={() => {selectStage(null); setPlaying(false);}}>Follow live progress</button>}
      <button className="rounded-lg bg-emerald-950 px-3 py-2 text-white disabled:opacity-40" disabled={stage === 5} onClick={() => choose(stage + 1)}>Next stage →</button>
    </footer>
  </section>;
}
