export function shouldAutoOpenHistory(submissionState?: string | null): boolean {
  return String(submissionState ?? "").toUpperCase() === "ACCEPTED";
}

export function launchHistorySearch(launchId: string): string {
  return `?launchId=${encodeURIComponent(launchId)}`;
}

export function focusedLaunchIdFromSearch(search: string): string {
  const value = new URLSearchParams(search).get("launchId")?.trim() ?? "";
  return value.length <= 256 ? value : "";
}
