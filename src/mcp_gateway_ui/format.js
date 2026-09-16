// How a gateway fact is written down, for the two places that write it down.
//
// These were local to `app.js` until a second surface needed them. Uptime is on the footer
// meta line *and* on the About screen, and a duration that reads `2h 14m` in one place and
// `8040s` in the other is the kind of difference a person notices and cannot explain. A
// shared module is the only version of that promise a test could ever check.
//
// This is deliberately not a `util.js`. What is in here is display formatting of the fields
// `admin.status` answers with, and that is the whole admissible list -- the moment something
// arrives that is not that, it wants its own file rather than this one's drawer. The 12-line
// `el` helper stays copied into each module that builds DOM, which is the existing house
// rule: a DOM shim never drifts, and a formatting decision does.

/** The last segment of a path. The full thing goes on a `title`; see `updateFiles`. */
export const basename = (path) => String(path).split('/').pop() || String(path);

/**
 * A duration in seconds, at one unit of precision until it needs two.
 *
 * Coarse on purpose. This answers "has it been up since I last looked?", and a running
 * total to the second is a number that changes every time it is painted while telling
 * nobody anything they were asking.
 */
export const formatDuration = (seconds) => {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
};
