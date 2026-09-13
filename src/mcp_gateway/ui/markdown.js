// The Markdown the Clipboard modal previews, rendered to DOM nodes.
//
// The briefing in that modal is Markdown — `clipboard.py` writes headings, fences, lists
// and emphasis — and until there was a preview the only way to see it as a reader would
// was to paste it somewhere else first. This is the renderer behind that toggle.
//
// **It builds nodes, never HTML.** There is no `innerHTML` here and no escaping function
// either, because nothing is ever parsed as markup: text goes into text nodes, which
// cannot become an element however it is spelled. That is not general caution, it is the
// specific rule the whole document runs on — everything under Results came off a backend
// this gateway launched, and a preview that honoured `<img onerror=…>` in a tool result
// would turn quoted evidence into script running on the admin page. For the same reason
// there are no links and no images: a rendered `[click](javascript:…)` is a payload, and
// the person previewing wanted to read their document, not navigate out of it.
//
// **It renders the subset the document uses**, listed just below, and shows anything
// else as the literal text it is. A preview that silently dropped a construct would be
// worse than one that shows it raw: the text in the box is what gets pasted, and the
// preview's only job is to say what that text will look like. See `clipboard.md`.

const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
};

//: The block constructs this understands, and the whole of what it understands. Everything else is a paragraph of literal text.
//: - ATX headings, `#` to `######`
//: - fenced code, three backticks or more — `clipboard.py` opens payloads with four so a
//:   backend answering in Markdown cannot close the fence from inside, and the close here
//:   is matched to the open's length for exactly that reason
//: - `-` and `*` bullet lists
//: - blank-line-separated paragraphs
//: Anything else -- tables, block quotes, numbered lists, links -- is shown literally.

const HEADING = /^(#{1,6})\s+(.*)$/;
const FENCE = /^(`{3,})\s*([^`]*)$/;
const BULLET = /^[-*]\s+(.*)$/;

/**
 * `text` as a `DocumentFragment`, ready to append.
 *
 * A fragment rather than a container element: the caller owns the box this lands in, its
 * class and its scroll position, and a renderer that also decided the wrapper would be
 * deciding half of the caller's layout.
 */
export function renderMarkdown(text) {
  const out = document.createDocumentFragment();
  const lines = String(text ?? '').split('\n');
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i += 1; continue; }

    const fence = FENCE.exec(line.trim());
    if (fence) {
      const [, ticks, info] = fence;
      const body = [];
      i += 1;
      // An unclosed fence runs to the end of the document rather than being abandoned:
      // `clipboard.py` truncates payloads, and a cut that lands mid-fence is a thing that
      // actually happens. Showing the rest as code is closer to the author's intent than
      // showing it as prose.
      while (i < lines.length && !new RegExp(`^${ticks}\`*\\s*$`).test(lines[i].trim())) {
        body.push(lines[i]);
        i += 1;
      }
      // Trailing blank lines belong to a fence that was closed; on one that ran off the
      // end they are the split's artifact, not the payload's.
      if (i >= lines.length) while (body.length && !body[body.length - 1].trim()) body.pop();
      i += 1;
      const code = el('code', { text: body.join('\n') });
      if (info.trim()) code.setAttribute('data-lang', info.trim());
      out.append(el('pre', { class: 'md-pre' }, code));
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      const [, hashes, body] = heading;
      out.append(inlineInto(el(`h${hashes.length}`, { class: 'md-h' }), body));
      i += 1;
      continue;
    }

    if (BULLET.test(line)) {
      const list = el('ul', { class: 'md-ul' });
      while (i < lines.length && BULLET.test(lines[i])) {
        list.append(inlineInto(el('li'), BULLET.exec(lines[i])[1]));
        i += 1;
      }
      out.append(list);
      continue;
    }

    // A paragraph runs to the next blank line or block opener. Its lines are joined with a
    // space, which is what Markdown does with a soft break and what keeps the preamble's
    // wrapped sentences reading as sentences.
    const paragraph = [];
    while (i < lines.length && lines[i].trim()
           && !HEADING.test(lines[i]) && !FENCE.test(lines[i].trim()) && !BULLET.test(lines[i])) {
      paragraph.push(lines[i].trim());
      i += 1;
    }
    out.append(inlineInto(el('p', { class: 'md-p' }), paragraph.join(' ')));
  }

  return out;
}

//: Inline spans, in precedence order. Code first and unconditionally: a backtick span is
//: opaque in Markdown, and the document is full of `**` and `_` inside identifiers that
//: only stay literal because the span around them wins.
const INLINE = /(`+)([\s\S]*?)\1|(\*\*|__)([\s\S]+?)\3|(\*|_)([^*_]+?)\5/;

/** Parse `text` as inline Markdown and append the result to `node`. Returns `node`. */
function inlineInto(node, text) {
  let rest = String(text ?? '');

  while (rest) {
    const match = INLINE.exec(rest);
    if (!match) break;

    const [whole, , code, strongMark, strongBody, emMark, emBody] = match;
    // Intra-word underscores are not emphasis -- `gateway__clipboard` in prose is one
    // word, not a bold `clipboard`. CommonMark's flanking rule, reduced to the half of it
    // that matters here: an underscore run glued to word characters on both sides opens
    // nothing. Asterisks are exempt, which is what CommonMark says too.
    const mark = strongMark || emMark;
    if (mark && mark[0] === '_'
        && /\w$/.test(rest.slice(0, match.index))
        && /^\w/.test(rest.slice(match.index + whole.length))) {
      // Step past this run and keep looking, rather than dropping the rest of the line.
      const consumed = match.index + whole.length;
      node.append(document.createTextNode(rest.slice(0, consumed)));
      rest = rest.slice(consumed);
      continue;
    }

    if (match.index) node.append(document.createTextNode(rest.slice(0, match.index)));

    if (code !== undefined) node.append(el('code', { class: 'md-code', text: code }));
    else if (strongBody !== undefined) node.append(inlineInto(el('strong'), strongBody));
    else node.append(inlineInto(el('em'), emBody));

    rest = rest.slice(match.index + whole.length);
  }

  if (rest) node.append(document.createTextNode(rest));
  return node;
}
