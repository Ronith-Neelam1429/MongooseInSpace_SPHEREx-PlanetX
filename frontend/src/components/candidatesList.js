// Pure state updates of the Candidates panel list, kept apart from the component for tests.

/**
 * The list after a "Show more" answer `r` (null: the route is gone) to a request made for
 * `requested`. An answer for another filter than the one now shown (the filter changed while it
 * was loading) is dropped, so one filter's page never lands in another's list.
 */
export function mergePage(l, r, requested) {
  if (l.status !== 'ok' || l.filter !== requested) return l;
  if (!r) return { ...l, loadingMore: false };
  return { status: 'ok', filter: requested, total: r.total, candidates: [...l.candidates, ...r.candidates] };
}

/** The list after a "Show more" request for `requested` failed; ignored when the filter changed. */
export function failPage(l, error, requested) {
  if (l.status !== 'ok' || l.filter !== requested) return l;
  return { status: 'error', error };
}
