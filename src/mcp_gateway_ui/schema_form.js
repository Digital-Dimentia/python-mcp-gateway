// A form built from a tool's own JSON Schema.
//
// The rules here are not invented. They are the contract python-acp writes down in
// `docs/tool-schema-contract.md`, which exists so that an MCP server author knows what a
// good schema buys their user and a client knows what it may rely on. The schema zoo in
// `examples/zoo_server.py` has one tool per construct, and it is the fixture this file is
// answerable to.
//
// ## Three states, not two
//
//   properties with entries  ->  a form
//   properties: {}           ->  "this tool takes no parameters" — a statement
//   no inputSchema at all    ->  a raw JSON box and no claim about parameters
//
// The last two are deliberately not merged: reporting an absent schema as "takes no
// parameters" asserts a fact nobody published.
//
// ## Decline rather than half-render
//
// `if`/`then`/`else`, `dependentSchemas`, `allOf` and discriminated `oneOf` step aside to
// a raw JSON box carrying the reason. A form that renders a subset of a conditional schema
// as though it were the whole thing is confidently wrong, which is worse than the text box
// it should have fallen back to.
//
// ## Validation here is a convenience, never a trust boundary
//
// Every form shows the exact JSON it will send and will send it anyway on request. The
// gateway and the backend are the authority; a client-side form that let either skip a
// check would be a regression in exactly the direction that matters.
//
// ## Empty means omitted
//
// An untouched optional field is left out of `arguments` entirely — not sent as `""`, not
// sent as `null`. `{"tags": []}` and `{}` say different things to a server, and a form
// that cannot say the second one is a form that cannot express most calls. Booleans get a
// three-way select for the same reason: an unticked checkbox cannot mean "not provided".

/** Keywords whose presence means this form steps aside. See the module comment. */
const DECLINED = {
  if: 'if/then/else',
  then: 'if/then/else',
  else: 'if/then/else',
  dependentSchemas: 'dependentSchemas',
  allOf: 'allOf',
  not: 'not',
};

/** How deep object nesting is rendered before falling back to a JSON box. */
const MAX_DEPTH = 3;

/**
 * What kind of input surface a schema deserves. Pure; exported for its own sake.
 * @returns {{mode: 'form'|'none'|'raw', reason: string|null, schema: object|null}}
 */
export function analyse(schema) {
  if (schema === undefined || schema === null) {
    return {
      mode: 'raw',
      reason: 'This tool publishes no inputSchema, so nothing is known about its parameters.',
      schema: null,
    };
  }
  if (typeof schema !== 'object' || Array.isArray(schema)) {
    return { mode: 'raw', reason: 'inputSchema is not an object.', schema: null };
  }
  const declined = declinedKeyword(schema);
  if (declined) {
    return {
      mode: 'raw',
      reason: `This schema uses ${declined}, which this form does not render — a partial `
        + 'rendering of a conditional schema would be confidently wrong. Send the arguments '
        + 'as JSON instead.',
      schema,
    };
  }
  const properties = schema.properties;
  if (properties === undefined) {
    return {
      mode: 'raw',
      reason: 'This schema declares no properties block, so it says nothing about parameters.',
      schema,
    };
  }
  if (typeof properties !== 'object' || Array.isArray(properties)) {
    return { mode: 'raw', reason: 'properties is not an object.', schema };
  }
  if (Object.keys(properties).length === 0) {
    return { mode: 'none', reason: 'This tool takes no parameters.', schema };
  }
  return { mode: 'form', reason: null, schema };
}

/** The declined keyword a schema carries, as a label, or null. */
function declinedKeyword(schema) {
  for (const [key, label] of Object.entries(DECLINED)) {
    if (key in schema) return label;
  }
  // A `oneOf` of nothing but `const` branches is a choice with labels, which renders fine.
  // A `oneOf` of sub-schemas is a discriminated union, which does not.
  if (Array.isArray(schema.oneOf) && !schema.oneOf.every(isConstBranch)) return 'oneOf';
  if (Array.isArray(schema.anyOf) && !schema.anyOf.every(isConstBranch)) return 'anyOf';
  return null;
}

function isConstBranch(branch) {
  return branch && typeof branch === 'object' && 'const' in branch;
}

/** The choices a schema offers, as [{value, label}], or null when it offers none. */
function choicesOf(schema) {
  if (Array.isArray(schema.enum)) {
    const names = Array.isArray(schema.enumNames) ? schema.enumNames : [];
    return schema.enum.map((value, i) => ({ value, label: String(names[i] ?? format(value)) }));
  }
  for (const key of ['oneOf', 'anyOf']) {
    const branches = schema[key];
    if (Array.isArray(branches) && branches.length && branches.every(isConstBranch)) {
      return branches.map((b) => ({ value: b.const, label: String(b.title ?? format(b.const)) }));
    }
  }
  return null;
}

function format(value) {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/** The single `type` a schema names, or null for none / a union of several. */
function soleType(schema) {
  const t = schema.type;
  return typeof t === 'string' ? t : null;
}

// ── Rendering ──────────────────────────────────────────────────────────────────

const el = (tag, props = {}, children = []) => {
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

/** Thrown by `collect` when the form cannot produce arguments at all. */
export class FormError extends Error {}

/**
 * Build the input surface for a schema.
 * @returns {{element: HTMLElement, mode: string, collect: () => object,
 *            problems: () => string[]}}
 *   `collect` throws FormError only when the input is unreadable (bad JSON). Everything
 *   else — a missing required field, a value out of range — comes back from `problems`
 *   as advice, and the caller may send anyway.
 */
export function buildForm(schema, { onChange = () => {} } = {}) {
  const verdict = analyse(schema);

  if (verdict.mode === 'none') {
    return {
      element: el('p', { class: 'note', text: verdict.reason }),
      mode: 'none',
      collect: () => ({}),
      problems: () => [],
    };
  }

  if (verdict.mode === 'raw') {
    const area = el('textarea', {
      class: 'raw-json',
      rows: 6,
      spellcheck: false,
      placeholder: '{}',
    });
    area.addEventListener('input', onChange);
    return {
      element: el('div', { class: 'field field-raw' }, [
        el('p', { class: 'note', text: verdict.reason }),
        area,
      ]),
      mode: 'raw',
      collect: () => {
        const text = area.value.trim();
        if (!text) return {};
        let parsed;
        try {
          parsed = JSON.parse(text);
        } catch (err) {
          throw new FormError(`arguments are not valid JSON: ${err.message}`);
        }
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new FormError('arguments must be a JSON object');
        }
        return parsed;
      },
      problems: () => [],
    };
  }

  const required = new Set(Array.isArray(schema.required) ? schema.required : []);
  const dependentRequired = schema.dependentRequired || {};
  const wrap = el('div', { class: 'fields' });
  const fields = [];

  for (const [name, propertySchema] of Object.entries(schema.properties)) {
    const field = renderField(name, propertySchema || {}, {
      required: required.has(name),
      depth: 1,
      onChange: () => { applyDependents(); onChange(); },
    });
    fields.push(field);
    wrap.append(field.element);
  }

  // `dependentRequired: {card: ["expiry"]}` — naming one property makes another required.
  // The only conditional construct rendered rather than declined, because it changes a
  // marker and nothing else: no field appears, disappears, or changes shape.
  function applyDependents() {
    const present = new Set(fields.filter((f) => f.hasValue()).map((f) => f.name));
    const implied = new Set();
    for (const [trigger, names] of Object.entries(dependentRequired)) {
      if (present.has(trigger)) for (const n of names || []) implied.add(n);
    }
    for (const field of fields) field.setImpliedRequired(implied.has(field.name));
  }
  applyDependents();

  return {
    element: wrap,
    mode: 'form',
    collect() {
      const args = {};
      for (const field of fields) {
        const value = field.collect();
        if (value !== OMIT) args[field.name] = value;
      }
      return args;
    },
    problems() {
      const found = [];
      for (const field of fields) found.push(...field.problems());
      return found;
    },
  };
}

/** The sentinel a field returns for "leave me out of the arguments entirely". */
const OMIT = Symbol('omit');

function renderField(name, schema, { required, depth, onChange }) {
  const title = schema.title || name;
  const choices = choicesOf(schema);
  const type = soleType(schema);
  let impliedRequired = false;

  const label = el('label', { class: 'field-label' }, [
    el('span', { class: 'field-name', text: title }),
    el('code', { class: 'field-key', text: name }),
  ]);
  const mark = el('span', { class: 'req', text: 'required', hidden: !required });
  label.append(mark);

  const control = buildControl(name, schema, { choices, type, depth, onChange, required });

  const parts = [label, control.element];
  if (schema.description) parts.push(el('p', { class: 'field-help', text: schema.description }));
  const hint = constraintHint(schema);
  if (hint) parts.push(el('p', { class: 'field-hint', text: hint }));
  const warn = el('p', { class: 'field-problem', hidden: true });
  parts.push(warn);

  // `data-field` is how the variables column finds the control a value belongs in. It
  // carries the *wire* name, not the title: that is what the vocabulary names.
  const element = el('div', {
    class: `field field-${control.kind}`, dataset: { field: name },
  }, parts);

  const isRequired = () => required || impliedRequired;

  function problems() {
    const found = [];
    const value = safeCollect();
    if (value === OMIT) {
      if (isRequired()) found.push(`${name} is required`);
    } else {
      found.push(...(control.problems ? control.problems(value) : []));
    }
    warn.textContent = found.join('; ');
    warn.hidden = found.length === 0;
    return found;
  }

  function safeCollect() {
    try {
      return control.collect();
    } catch (err) {
      warn.textContent = err.message;
      warn.hidden = false;
      throw err;
    }
  }

  return {
    name,
    element,
    collect: safeCollect,
    problems,
    hasValue: () => {
      try {
        return control.collect() !== OMIT;
      } catch {
        return true;
      }
    },
    setImpliedRequired(value) {
      impliedRequired = value;
      mark.hidden = !(required || value);
      mark.textContent = required ? 'required' : 'required (implied)';
    },
  };
}

/** One control, chosen from what the schema actually says. */
function buildControl(name, schema, { choices, type, depth, onChange, required }) {
  // A single legal value is not a choice. Show it, send it, do not offer a dropdown of one.
  if ('const' in schema) {
    const input = el('input', { type: 'text', value: format(schema.const), readOnly: true });
    return {
      kind: 'const',
      element: input,
      collect: () => schema.const,
    };
  }

  if (choices) {
    const select = el('select', {}, [el('option', { value: '', text: '(omit)' })]);
    for (const [i, choice] of choices.entries()) {
      // `dataset.value` beside the index: the index is what `collect` needs (see below) and
      // the choice's own spelling is what anything *outside* the form has to match against.
      // The variables column offers values, not positions, so `putValue` looks here.
      select.append(el('option', {
        value: String(i), text: choice.label, dataset: { value: format(choice.value) },
      }));
    }
    if (schema.default !== undefined) {
      const at = choices.findIndex((c) => JSON.stringify(c.value) === JSON.stringify(schema.default));
      if (at >= 0) select.value = String(at);
    }
    select.addEventListener('change', onChange);
    return {
      kind: 'choice',
      element: select,
      // The index, not the label: `{"enum": [0, 1, 3, 5]}` must come back as the number 3,
      // not the string "3". A select's value is always a string, so the value has to be
      // carried out of band.
      collect: () => (select.value === '' ? OMIT : choices[Number(select.value)].value),
    };
  }

  if (type === 'boolean') {
    // Three-way, not a checkbox: an unticked box cannot mean "not provided", and silently
    // sending `false` for every optional boolean is how a form invents arguments.
    const select = el('select', {}, [
      el('option', { value: '', text: '(omit)' }),
      el('option', { value: 'true', text: 'true' }),
      el('option', { value: 'false', text: 'false' }),
    ]);
    if (typeof schema.default === 'boolean') select.value = String(schema.default);
    select.addEventListener('change', onChange);
    return {
      kind: 'boolean',
      element: select,
      collect: () => (select.value === '' ? OMIT : select.value === 'true'),
    };
  }

  if (type === 'null') {
    const select = el('select', {}, [
      el('option', { value: '', text: '(omit)' }),
      el('option', { value: 'null', text: 'null' }),
    ]);
    select.addEventListener('change', onChange);
    return {
      kind: 'null',
      element: select,
      collect: () => (select.value === '' ? OMIT : null),
    };
  }

  if (type === 'number' || type === 'integer') {
    const input = el('input', {
      type: 'number',
      step: schema.multipleOf ?? (type === 'integer' ? 1 : 'any'),
      min: schema.minimum ?? schema.exclusiveMinimum,
      max: schema.maximum ?? schema.exclusiveMaximum,
      placeholder: schema.default !== undefined ? String(schema.default) : '',
    });
    if (schema.default !== undefined) input.value = String(schema.default);
    input.addEventListener('input', onChange);
    return {
      kind: 'number',
      element: input,
      collect: () => {
        const raw = input.value.trim();
        if (raw === '') return OMIT;
        const value = Number(raw);
        if (!Number.isFinite(value)) throw new FormError(`${name} is not a number`);
        return value;
      },
      problems: (value) => numberProblems(name, value, schema, type),
    };
  }

  if (type === 'array') {
    return buildArrayControl(name, schema, { depth, onChange });
  }

  if (type === 'object') {
    const nested = schema.properties && Object.keys(schema.properties).length;
    if (nested && depth < MAX_DEPTH && !declinedKeyword(schema)) {
      return buildObjectControl(name, schema, { depth, onChange });
    }
    return jsonControl(name, schema, onChange, nested
      ? 'Nested too deep to render as a form.'
      : 'This object declares no properties.');
  }

  if (type === 'string') {
    const long = (schema.maxLength ?? 0) > 120;
    const input = long
      ? el('textarea', { rows: 4, spellcheck: false })
      : el('input', { type: inputTypeFor(schema.format), spellcheck: false });
    if (schema.default !== undefined) input.value = String(schema.default);
    if (schema.pattern) input.title = `must match ${schema.pattern}`;
    input.addEventListener('input', onChange);
    return {
      kind: 'string',
      element: input,
      collect: () => (input.value === '' ? OMIT : input.value),
      problems: (value) => stringProblems(name, value, schema),
    };
  }

  // No type, or a union of several. Read it as JSON and keep it as a string when that
  // fails, which is the guess a person typing a value expects — and only a guess.
  // Declaring a type is what would make it a fact.
  const input = el('input', { type: 'text', spellcheck: false });
  if (schema.default !== undefined) input.value = format(schema.default);
  input.addEventListener('input', onChange);
  const types = Array.isArray(schema.type) ? schema.type.join(' or ') : 'no declared type';
  return {
    kind: 'untyped',
    element: el('div', {}, [
      input,
      el('p', { class: 'field-hint', text: `${types} — read as JSON, kept as text if that fails` }),
    ]),
    collect: () => {
      const raw = input.value.trim();
      if (raw === '') return OMIT;
      try {
        return JSON.parse(raw);
      } catch {
        return input.value;
      }
    },
  };
}

function inputTypeFor(format) {
  switch (format) {
    case 'date': return 'date';
    case 'date-time': return 'datetime-local';
    case 'time': return 'time';
    case 'email': return 'email';
    case 'uri':
    case 'url': return 'url';
    case 'password': return 'password';
    // An unrecognised format degrades to a text box. It must not be dropped and must not
    // be refused — `zoo-strings` publishes `x-not-a-real-format` to catch exactly that.
    default: return 'text';
  }
}

function buildArrayControl(name, schema, { depth, onChange }) {
  const items = schema.items;
  const itemChoices = items ? choicesOf(items) : null;

  // An array of enums is a multi-select, which is a genuinely different widget from a
  // list of rows and the one a user expects for "pick some kinds".
  if (itemChoices) {
    const box = el('div', { class: 'multi' });
    const inputs = itemChoices.map((choice, i) => {
      const check = el('input', { type: 'checkbox', value: String(i) });
      check.addEventListener('change', onChange);
      box.append(el('label', { class: 'multi-item' }, [check, el('span', { text: choice.label })]));
      return check;
    });
    return {
      kind: 'multi',
      element: box,
      collect: () => {
        const chosen = inputs
          .map((input, i) => (input.checked ? itemChoices[i].value : undefined))
          .filter((v) => v !== undefined);
        // Nothing ticked is "not provided", not "the empty list". Sending `[]` would be a
        // claim the user never made; a schema that wants an explicit empty list can be
        // spelt in the raw JSON box.
        return chosen.length ? chosen : OMIT;
      },
      problems: (value) => arrayProblems(name, value, schema),
    };
  }

  if (!items || declinedKeyword(items)) {
    return jsonControl(name, schema, onChange,
      items ? 'Item schema uses a construct this form declines.'
        : 'This array declares no items schema, so there is no row to build.');
  }

  const rows = el('div', { class: 'rows' });
  const controls = [];
  const addRow = () => {
    const control = buildControl(`${name}[]`, items, {
      choices: null, type: soleType(items), depth: depth + 1, onChange, required: false,
    });
    const row = el('div', { class: 'row' }, [
      control.element,
      el('button', { type: 'button', class: 'ghost row-del', text: '×', title: 'Remove' }),
    ]);
    row.querySelector('.row-del').addEventListener('click', () => {
      controls.splice(controls.indexOf(control), 1);
      row.remove();
      onChange();
    });
    controls.push(control);
    rows.append(row);
    onChange();
  };
  const add = el('button', { type: 'button', class: 'ghost', text: '+ Add item' });
  add.addEventListener('click', addRow);

  return {
    kind: 'array',
    element: el('div', {}, [rows, add]),
    collect: () => {
      const values = controls.map((c) => c.collect()).filter((v) => v !== OMIT);
      return controls.length === 0 ? OMIT : values;
    },
    problems: (value) => arrayProblems(name, value, schema),
  };
}

function buildObjectControl(name, schema, { depth, onChange }) {
  const required = new Set(Array.isArray(schema.required) ? schema.required : []);
  const fields = [];
  const box = el('div', { class: 'nested' });
  for (const [key, sub] of Object.entries(schema.properties)) {
    const field = renderField(key, sub || {}, {
      required: required.has(key),
      depth: depth + 1,
      onChange,
    });
    fields.push(field);
    box.append(field.element);
  }
  return {
    kind: 'object',
    element: box,
    collect: () => {
      const value = {};
      for (const field of fields) {
        const v = field.collect();
        if (v !== OMIT) value[field.name] = v;
      }
      // An object nobody filled in is absent, not `{}`.
      return Object.keys(value).length ? value : OMIT;
    },
    problems: () => fields.flatMap((f) => f.problems()),
  };
}

function jsonControl(name, schema, onChange, why) {
  const area = el('textarea', { class: 'raw-json', rows: 3, spellcheck: false, placeholder: 'JSON' });
  area.addEventListener('input', onChange);
  return {
    kind: 'json',
    element: el('div', {}, [area, el('p', { class: 'field-hint', text: why })]),
    collect: () => {
      const raw = area.value.trim();
      if (raw === '') return OMIT;
      try {
        return JSON.parse(raw);
      } catch (err) {
        throw new FormError(`${name} is not valid JSON: ${err.message}`);
      }
    },
  };
}

// ── Advice. Never enforcement — see the module comment. ────────────────────────

function constraintHint(schema) {
  const bits = [];
  if (schema.minimum !== undefined) bits.push(`≥ ${schema.minimum}`);
  if (schema.maximum !== undefined) bits.push(`≤ ${schema.maximum}`);
  if (schema.exclusiveMinimum !== undefined) bits.push(`> ${schema.exclusiveMinimum}`);
  if (schema.exclusiveMaximum !== undefined) bits.push(`< ${schema.exclusiveMaximum}`);
  if (schema.multipleOf !== undefined) bits.push(`multiple of ${schema.multipleOf}`);
  if (schema.minLength !== undefined) bits.push(`≥ ${schema.minLength} chars`);
  if (schema.maxLength !== undefined) bits.push(`≤ ${schema.maxLength} chars`);
  if (schema.pattern) bits.push(`matches ${schema.pattern}`);
  if (schema.minItems !== undefined) bits.push(`≥ ${schema.minItems} items`);
  if (schema.maxItems !== undefined) bits.push(`≤ ${schema.maxItems} items`);
  if (schema.format) bits.push(schema.format);
  if (schema.default !== undefined) bits.push(`default ${format(schema.default)}`);
  return bits.join(' · ');
}

function numberProblems(name, value, schema, type) {
  const out = [];
  if (type === 'integer' && !Number.isInteger(value)) out.push(`${name} must be an integer`);
  if (schema.minimum !== undefined && value < schema.minimum) out.push(`${name} < ${schema.minimum}`);
  if (schema.maximum !== undefined && value > schema.maximum) out.push(`${name} > ${schema.maximum}`);
  if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
    out.push(`${name} must be > ${schema.exclusiveMinimum}`);
  }
  if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) {
    out.push(`${name} must be < ${schema.exclusiveMaximum}`);
  }
  if (schema.multipleOf) {
    // Float modulo, so compare against a tolerance rather than zero: 0.75 % 0.25 is not
    // exactly 0 in binary floating point, and a form that says so is wrong about `0.75`.
    const ratio = value / schema.multipleOf;
    if (Math.abs(ratio - Math.round(ratio)) > 1e-9) {
      out.push(`${name} must be a multiple of ${schema.multipleOf}`);
    }
  }
  return out;
}

function stringProblems(name, value, schema) {
  const out = [];
  if (schema.minLength !== undefined && value.length < schema.minLength) {
    out.push(`${name} is shorter than ${schema.minLength}`);
  }
  if (schema.maxLength !== undefined && value.length > schema.maxLength) {
    out.push(`${name} is longer than ${schema.maxLength}`);
  }
  if (schema.pattern) {
    let re = null;
    // A schema's pattern is ECMA-262 by spec but servers write Python and Go regexes too.
    // One this engine cannot compile is not the user's problem, and not grounds to block
    // a call: skip the check rather than report a failure the server never asked for.
    try { re = new RegExp(schema.pattern); } catch { re = null; }
    if (re && !re.test(value)) out.push(`${name} does not match ${schema.pattern}`);
  }
  return out;
}

function arrayProblems(name, value, schema) {
  const out = [];
  if (!Array.isArray(value)) return out;
  if (schema.minItems !== undefined && value.length < schema.minItems) {
    out.push(`${name} needs ≥ ${schema.minItems} items`);
  }
  if (schema.maxItems !== undefined && value.length > schema.maxItems) {
    out.push(`${name} allows ≤ ${schema.maxItems} items`);
  }
  return out;
}

/** Prompts are not JSON Schema: `arguments[]` is name/description/required and nothing else. */
export function buildPromptForm(promptArguments, { onChange = () => {} } = {}) {
  const args = Array.isArray(promptArguments) ? promptArguments : [];
  if (!args.length) {
    return {
      element: el('p', { class: 'note', text: 'This prompt takes no arguments.' }),
      mode: 'none',
      collect: () => ({}),
      problems: () => [],
    };
  }
  const wrap = el('div', { class: 'fields' });
  const inputs = [];
  for (const arg of args) {
    const input = el('input', { type: 'text', spellcheck: false });
    input.addEventListener('input', onChange);
    inputs.push({ name: arg.name, input, required: !!arg.required });
    const label = el('label', { class: 'field-label' }, [
      el('span', { class: 'field-name', text: arg.title || arg.name }),
      el('code', { class: 'field-key', text: arg.name }),
    ]);
    if (arg.required) label.append(el('span', { class: 'req', text: 'required' }));
    const parts = [label, input];
    if (arg.description) parts.push(el('p', { class: 'field-help', text: arg.description }));
    wrap.append(el('div', { class: 'field field-string', dataset: { field: arg.name } }, parts));
  }
  return {
    element: wrap,
    mode: 'form',
    // The controls themselves, so a caller can hang something off each one without going
    // back through the DOM for them. `app.js` attaches completion suggestions here; this
    // module stays as ignorant of MCP requests as it was.
    fields: inputs.map(({ name, input }) => ({ name, input })),
    collect: () => {
      const out = {};
      // Prompt arguments are strings on the wire — `prompts/get` declares
      // `arguments: {[key: string]: string}` — so nothing is coerced here.
      for (const { name, input } of inputs) if (input.value !== '') out[name] = input.value;
      return out;
    },
    problems: () => inputs
      .filter(({ input, required }) => required && input.value === '')
      .map(({ name }) => `${name} is required`),
  };
}

/** A URI template's `{var}` names, in order, deduplicated. RFC 6570's simple form. */
export function templateVariables(uriTemplate) {
  const names = [];
  for (const match of String(uriTemplate || '').matchAll(/\{([^}]*)\}/g)) {
    // Strip the operator prefix (`+`, `#`, `?`, `&`, …) and any explode/prefix modifier,
    // so `{?q,lang}` yields `q` and `lang` rather than one impossible name.
    for (const raw of match[1].replace(/^[+#./;?&]/, '').split(',')) {
      const name = raw.replace(/[*:].*$/, '').trim();
      if (name && !names.includes(name)) names.push(name);
    }
  }
  return names;
}

/** Expand a template against `values`. Unnamed variables expand to empty, as RFC 6570 says. */
export function expandTemplate(uriTemplate, values) {
  return String(uriTemplate || '').replace(/\{([^}]*)\}/g, (_, expr) => {
    const operator = /^[+#./;?&]/.test(expr) ? expr[0] : '';
    const body = operator ? expr.slice(1) : expr;
    const parts = body.split(',').map((raw) => {
      const name = raw.replace(/[*:].*$/, '').trim();
      const value = values[name];
      if (value === undefined || value === '') return null;
      // `+` and `#` are the reserved-expansion operators: they pass `/` and `:` through,
      // which is what a template naming a path segment needs.
      const encoded = operator === '+' || operator === '#'
        ? encodeURI(String(value))
        : encodeURIComponent(String(value));
      return operator === '?' || operator === '&' ? `${name}=${encoded}` : encoded;
    }).filter((p) => p !== null);
    if (!parts.length) return '';
    const prefix = operator === '#' || operator === '.' || operator === '/'
      || operator === '?' || operator === '&' ? operator : '';
    const separator = operator === '?' || operator === '&' ? '&'
      : operator === '/' ? '/' : operator === '.' ? '.' : ',';
    return prefix + parts.join(separator);
  });
}
