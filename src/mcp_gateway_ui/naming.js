// The gateway's namespacing, undone. Mirrors `naming.py`, and only that far.
//
// The gateway publishes a backend's tool as `server__tool` and its resource as
// `mcpgw://server/<the backend's own URI, percent-encoded>`. Every column that shows a
// listing has to take that apart again — which server is this, and what did the backend
// itself call it — and for a while each of them did it with its own copy. This is that copy,
// written once.
//
// Pure functions over one entry, deliberately: nothing in here reads the page's state or
// touches the DOM, which is what lets the columns, the detail panel and the values column
// all import it directly rather than being handed it. The one function that needs to know
// which server is selected takes the server as an argument and lets its caller answer that.
//
// The round trip is all that has to agree with the daemon. `decode_resource_uri` unquotes
// whatever `encode_resource_uri` wrote, so neither side has to make the same choices about
// which characters to escape — see [`naming.md`](../mcp_gateway/naming.md).

//: A server name may contain neither, which is what makes a split on the first separator
//: unambiguous.
export const SEPARATOR = '__';
export const RESOURCE_SCHEME = 'mcpgw';

/** The backend a listing entry belongs to, or null when nothing namespaced it. */
export function ownerOf(kind, entry) {
  if (kind === 'tools' || kind === 'prompts') {
    const at = String(entry.name || '').indexOf(SEPARATOR);
    return at < 0 ? null : entry.name.slice(0, at);
  }
  const uri = String(entry.uri || entry.uriTemplate || '');
  const prefix = `${RESOURCE_SCHEME}://`;
  if (!uri.startsWith(prefix)) return null;
  return uri.slice(prefix.length).split('/')[0] || null;
}

/** The part after the namespace, which is what the backend itself called it. */
export function localName(kind, entry) {
  if (kind === 'tools' || kind === 'prompts') {
    const at = String(entry.name || '').indexOf(SEPARATOR);
    return at < 0 ? entry.name : entry.name.slice(at + SEPARATOR.length);
  }
  const uri = String(entry.uri || entry.uriTemplate || '');
  const prefix = `${RESOURCE_SCHEME}://${ownerOf(kind, entry) || ''}/`;
  if (!uri.startsWith(prefix)) return uri;
  // The gateway percent-encodes the backend's own URI into one path segment.
  try { return decodeURIComponent(uri.slice(prefix.length)); } catch { return uri.slice(prefix.length); }
}

/** What a listing entry is called, whichever of the three shapes it is. */
export const itemId = (entry) => entry.name || entry.uri || entry.uriTemplate;

/**
 * A backend's own URI in the gateway's address space.
 *
 * Mirrors `naming.encode_resource_uri`. What it is for is the cascade in the injectable
 * values column: a listing reached through another listing's `narrows` is a URI the *server*
 * produced and no listing publishes, so there is no entry to read its gateway spelling off.
 * Minting one is safe because `resources/read` resolves a URI rather than looking it up --
 * `Catalogue.find_resource` decodes the namespace and hands the rest to the backend, exactly
 * as it would for a template the client expanded itself.
 */
export function gatewayUri(server, local) {
  return `${RESOURCE_SCHEME}://${server}/${encodeURIComponent(local)}`;
}
