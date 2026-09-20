// Turn what came back into something a person can read, without ever pretending it is
// something else.
//
// Two rules run through everything here:
//
// **A blob is never decoded inline.** A resource with a `blob` is base64 of arbitrary
// bytes; printing it fills the column with noise, and decoding it to guess at text is a
// renderer inventing content. It renders as its mimeType and its size, with an explicit
// action to reveal or download. `zoo-blob` exists to catch a renderer that does otherwise.
//
// **Every card keeps the raw JSON.** A rendering is a convenience laid over the wire
// payload; the payload is the truth, it is one click away on every card, and `isError` is
// shown as what it is — a *successful* result carrying a tool-level failure, which is
// MCP's own contract and the thing most likely to be misread as a protocol error.

// Exported so the panel renderer builds its nodes with the same helper rather than a
// second copy: the no-innerHTML rule is only as good as the one way of making a node.
export const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    // Before the `k in node` branch: `dataset` is a readonly attribute, so assigning to it
    // throws in a module rather than setting the data-* attributes anyone asked for.
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
};

export const pretty = (value) => {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

/** A code block, pretty-printing the text when it happens to be JSON. */
function textBlock(text, { mimeType } = {}) {
  const trimmed = String(text ?? '');
  let body = trimmed;
  let language = mimeType || '';
  const looksJson = /^\s*[[{]/.test(trimmed);
  if (looksJson) {
    try {
      body = JSON.stringify(JSON.parse(trimmed), null, 2);
      language = 'application/json';
    } catch {
      // Not JSON after all. Show it exactly as it arrived.
    }
  }
  const pre = el('pre', { class: 'block' }, [el('code', { text: body })]);
  if (language) pre.dataset.lang = language;
  return pre;
}

function byteLength(base64) {
  const clean = String(base64 || '').replace(/[^A-Za-z0-9+/=]/g, '');
  if (!clean) return 0;
  const padding = (clean.match(/=+$/) || [''])[0].length;
  return Math.max(0, Math.floor((clean.length * 3) / 4) - padding);
}

const humanBytes = (n) => (n < 1024 ? `${n} B`
  : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KiB`
    : `${(n / 1024 / 1024).toFixed(1)} MiB`);

/** One MCP content block. Unknown types render as their JSON rather than vanishing. */
export function renderContent(block) {
  if (!block || typeof block !== 'object') return textBlock(String(block));
  switch (block.type) {
    case 'text':
      return textBlock(block.text);

    case 'image':
    case 'audio': {
      const source = `data:${block.mimeType || 'application/octet-stream'};base64,${block.data || ''}`;
      const media = block.type === 'image'
        ? el('img', { src: source, alt: block.mimeType || 'image', class: 'media' })
        : el('audio', { src: source, controls: true, class: 'media' });
      return el('figure', { class: 'content-media' }, [
        media,
        el('figcaption', {
          text: `${block.type} · ${block.mimeType || 'unknown'} · ${humanBytes(byteLength(block.data))}`,
        }),
      ]);
    }

    case 'resource':
      return el('div', { class: 'embedded' }, [
        el('div', { class: 'embedded-head', text: block.resource?.uri || 'embedded resource' }),
        renderResourceContents(block.resource),
      ]);

    case 'resource_link':
      return el('div', { class: 'embedded' }, [
        el('div', { class: 'embedded-head', text: block.uri || 'resource link' }),
        el('p', {
          class: 'note',
          text: [block.name, block.description, block.mimeType].filter(Boolean).join(' · ')
            || 'A link to a resource. Read it from the Resources tab.',
        }),
      ]);

    default:
      return el('div', {}, [
        el('p', { class: 'note', text: `Unrecognised content type ${block.type ?? '(none)'}` }),
        textBlock(pretty(block)),
      ]);
  }
}

/** One `contents[]` entry of a `resources/read`, or an embedded resource. */
export function renderResourceContents(entry) {
  if (!entry || typeof entry !== 'object') return textBlock(pretty(entry));
  if (typeof entry.text === 'string') return textBlock(entry.text, { mimeType: entry.mimeType });
  if (typeof entry.blob === 'string') {
    // Never decoded inline. See the module comment.
    const size = humanBytes(byteLength(entry.blob));
    const type = entry.mimeType || 'application/octet-stream';
    const box = el('div', { class: 'blob' }, [
      el('p', { text: `Binary content — ${type}, ${size}. Not shown.` }),
    ]);
    const link = el('a', {
      class: 'ghost button',
      href: `data:${type};base64,${entry.blob}`,
      download: (entry.uri || 'resource').split('/').pop() || 'resource.bin',
      text: 'Download',
    });
    box.append(link);
    if (type.startsWith('image/')) {
      const show = el('button', { type: 'button', class: 'ghost', text: 'Show image' });
      show.addEventListener('click', () => {
        show.replaceWith(el('img', { class: 'media', src: `data:${type};base64,${entry.blob}`, alt: type }));
      });
      box.append(show);
    }
    return box;
  }
  return textBlock(pretty(entry));
}

/** A `tools/call` result. */
export function renderToolResult(result) {
  const parts = [];
  if (result.isError) {
    parts.push(el('p', { class: 'banner banner-error' }, [
      el('strong', { text: 'isError: true' }),
      el('span', {
        text: ' — a successful result carrying a tool-level failure. This is MCP’s '
          + 'contract, not a protocol error: the model is meant to read it and adapt.',
      }),
    ]));
  }
  const content = Array.isArray(result.content) ? result.content : [];
  if (!content.length && result.structuredContent === undefined) {
    parts.push(el('p', { class: 'note', text: 'The tool returned no content.' }));
  }
  for (const block of content) parts.push(renderContent(block));
  if (result.structuredContent !== undefined) {
    parts.push(el('h4', { text: 'structuredContent' }));
    parts.push(textBlock(pretty(result.structuredContent)));
  }
  return el('div', { class: 'result-body' }, parts);
}

/** A `prompts/get` result: messages by role. */
export function renderPromptResult(result) {
  const parts = [];
  if (result.description) parts.push(el('p', { class: 'note', text: result.description }));
  const messages = Array.isArray(result.messages) ? result.messages : [];
  if (!messages.length) parts.push(el('p', { class: 'note', text: 'No messages.' }));
  for (const message of messages) {
    const blocks = Array.isArray(message.content) ? message.content : [message.content];
    parts.push(el('div', { class: 'message' }, [
      el('div', { class: 'message-role', text: message.role || 'unknown' }),
      el('div', { class: 'message-body' }, blocks.map(renderContent)),
    ]));
  }
  return el('div', { class: 'result-body' }, parts);
}

/** A `resources/read` result. */
export function renderResourceResult(result) {
  const contents = Array.isArray(result.contents) ? result.contents : [];
  if (!contents.length) {
    return el('div', { class: 'result-body' }, [
      el('p', { class: 'note', text: 'The resource returned no contents.' }),
    ]);
  }
  return el('div', { class: 'result-body' }, contents.map((entry) => el('div', { class: 'resource-part' }, [
    el('div', { class: 'resource-head' }, [
      el('code', { text: entry.uri || '(no uri)' }),
      entry.mimeType ? el('span', { class: 'tag', text: entry.mimeType }) : null,
    ]),
    renderResourceContents(entry),
  ])));
}

/** A JSON-RPC error, including the gateway's own `source` tag when it set one. */
export function renderError(error) {
  const parts = [
    el('p', { class: 'banner banner-fail' }, [
      el('strong', { text: `${error.code ?? '?'} ` }),
      el('span', { text: error.message || 'error' }),
    ]),
  ];
  if (error.data !== undefined && error.data !== null) {
    const source = error.data && typeof error.data === 'object' ? error.data.source : null;
    if (source) parts.push(el('p', { class: 'note', text: `source: ${source}` }));
    parts.push(textBlock(pretty(error.data)));
  }
  return el('div', { class: 'result-body' }, parts);
}

//: A trashcan, as path data. Built through `createElementNS` rather than `innerHTML`
//: because `el()` makes HTML elements and an `<svg>` in the HTML namespace does not draw --
//: a distinction that costs one helper and is invisible in the markup either way.
const TRASH = [
  'M2.5 4h11',                                       // the lid
  'M6 4V2.5h4V4',                                    // the handle above it
  'M4 4l.7 9a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9L12 4',// the tapering body
  'M6.6 6.7v4.6', 'M9.4 6.7v4.6',                    // the two ribs
];

function trashIcon() {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  for (const [name, value] of Object.entries({
    viewBox: '0 0 16 16', width: '13', height: '13', fill: 'none', stroke: 'currentColor',
    'stroke-width': '1.3', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true',
  })) svg.setAttribute(name, value);
  for (const d of TRASH) {
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

/**
 * One result card: what was asked, how long it took, and what came back.
 * `body` is a node; `request` is `{method, params}`.
 *
 * `onRemove` is called after the card takes itself out of the document, and is how the
 * column learns it may have just lost its last card. The card removes *itself* rather than
 * being handed its container: a card that knows where it lives is a card that can only live
 * there, and the callback says everything the caller actually needs told.
 */
export function resultCard({ title, subtitle, request, body, elapsedMs, raw, failed, onRemove }) {
  const rawText = pretty(raw);

  const copy = el('button', { type: 'button', class: 'ghost', text: 'Copy JSON' });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(rawText);
      copy.textContent = 'Copied';
      setTimeout(() => { copy.textContent = 'Copy JSON'; }, 1200);
    } catch {
      copy.textContent = 'Copy failed';
      setTimeout(() => { copy.textContent = 'Copy JSON'; }, 1200);
    }
  });

  const rawBlock = el('details', { class: 'raw' }, [
    el('summary', { text: 'Raw' }),
    el('h4', { text: 'Request' }),
    textBlock(pretty(request)),
    el('h4', { text: 'Response' }),
    textBlock(rawText),
  ]);

  const remove = el('button', {
    type: 'button', class: 'ghost danger card-del',
    title: 'Remove this result', 'aria-label': `Remove the ${title} result`,
  }, [trashIcon()]);

  const card = el('article', { class: `card${failed ? ' card-failed' : ''}` }, [
    el('header', { class: 'card-head' }, [
      el('div', {}, [
        el('h3', { text: title }),
        subtitle ? el('p', { class: 'card-sub', text: subtitle }) : null,
      ]),
      el('div', { class: 'card-meta' }, [
        el('span', { class: 'tag', text: `${Math.round(elapsedMs)} ms` }),
        copy,
        remove,
      ]),
    ]),
    body,
    rawBlock,
  ]);

  remove.addEventListener('click', () => {
    card.remove();
    onRemove?.();
  });
  return card;
}
