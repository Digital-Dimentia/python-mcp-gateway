// A form field whose schema names its own choices, and the value the column offers it.
//
// Its own file because `boot()` is once per process: see `harness.mjs`.

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';

import { boot, mcpAnswering, selectServer } from './harness.mjs';
import { BODIES, CONTINENTS, LISTINGS, ZOO, uri } from './zoo.mjs';

describe('a field whose schema names its choices', () => {
  let ui;
  let document;

  before(async () => {
    ({ ui, document } = await boot());
    selectServer(ui, ZOO, LISTINGS);
    mcpAnswering(ui, BODIES);
  });

  /** The same tool as above, rebuilt per test so each starts from `(omit)`. */
  const open = () => {
    const tool = {
      name: `${ZOO}__spot`,
      inputSchema: {
        type: 'object',
        properties: {
          continent: { type: 'string', enum: ['africa', 'asia'] },
          size: { type: 'integer', enum: [1, 3, 5] },
        },
      },
    };
    ui.openItem('tools', tool);
    return tool;
  };

  const selectFor = (name) => document.querySelector(`#detail [data-field="${name}"] select`);

  it('takes a value the schema offers, not the position it sits at', () => {
    // python-mcp-gateway-8df: `putValue` used to compare the pick against `option.value`,
    // which for an enum is the index -- so every such field rejected every value it had.
    open();
    assert.equal(ui.putValue(selectFor('continent'), 'asia'), null);
    assert.equal(selectFor('continent').selectedOptions[0].textContent, 'asia');
  });

  it('keeps the JSON type the schema asked for', () => {
    // The whole reason the index is the option's value: `{"enum": [1, 3, 5]}` has to send
    // the number 3, and a select's value is always a string.
    open();
    assert.equal(ui.putValue(selectFor('size'), '3'), null);
    // Asserted on the wire preview, which is the form's own answer to what it would send.
    const wire = JSON.parse(document.querySelector('#detail .preview code').textContent);
    assert.equal(wire.params.arguments.size, 3);
    assert.equal(typeof wire.params.arguments.size, 'number');
  });

  it('still says so about a value the field genuinely does not offer', () => {
    open();
    assert.match(ui.putValue(selectFor('continent'), 'antarctica'), /not one of the choices/);
    assert.equal(selectFor('continent').value, '', 'and leaves the field as it found it');
  });

  it('lets a pick made before the form was opened reach it', () => {
    // The two halves of this file's subject meeting: `applyPicks` writes the chain into a
    // form as it opens, and the field it writes into is a select whose options are indices.
    // `liveKeys` by hand because the column is not being resolved here -- `applyPicks` only
    // writes picks that a group is still on screen for, and no group is.
    const pick = ui.pickFor(uri(CONTINENTS), 'continent');
    pick.values = new Set(['africa']);
    ui.liveKeys.add(uri(CONTINENTS));
    open();
    assert.equal(selectFor('continent').selectedOptions[0].textContent, 'africa');
  });
});
