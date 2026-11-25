import type { Summary } from "./api";

export type SetupStep = { key: string; title: string; detail: string; label: string; href: string; done: boolean };

/** Workspace setup in order, worked out from the summary the dashboard already polls. */
export function setupSteps(s: Summary): SetupStep[] {
  return [
    { key: "site", title: "Add a site to monitor", detail: "OpsRelay checks it every few minutes and opens an incident when it fails.", label: "Add a site", href: "/sites", done: s.services.length > 0 },
    { key: "github", title: "Connect GitHub", detail: "Lets OpsRelay see the repositories your services run from.", label: "Connect GitHub", href: "/connectors",
      done: s.setup.github_connected },
    { key: "repo", title: "Map a repository to a service", detail: "Incidents then point at the right code.", label: "Add a repository", href: "/repositories",
      done: s.repositories.length > 0 },
    { key: "betterstack", title: "Connect Better Stack", detail: "Its alerts open incidents here.", label: "Connect Better Stack", href: "/connectors",
      done: s.setup.betterstack_configured },
    { key: "agent", title: "Turn on the repair agent", detail: "It investigates incidents and starts in report-only mode.", label: "Enable the agent", href: "/repositories",
      done: s.repositories.some((r) => r.agent.enabled) },
    { key: "notifications", title: "Get alerted", detail: "Send incidents to Slack, Discord, PagerDuty or a webhook.", label: "Set up alerts", href: "/connectors",
      done: s.setup.notifications_configured },
  ];
}
