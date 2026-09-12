// The fill path, in both directions.
//
// A pick and a field find each other two ways round, and until the cascade arrived only one
// of them was exercised. You used to open a tool and *then* click a value, so writing the
// value at the moment of the click was enough. You cannot pick a country before its
// continent, so now the picks are made first and the form is opened onto them -- and a form
// that applied only the fanned-out picks opened up empty. That shipped (4c8d117), and a
// browser is what found it. Hence both directions here.
//
// The other half of this file is the rule that makes filling a form safe at all: a pick
// goes in a field that carries its name, or it goes nowhere.

import { describe, it, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import { boot, fieldValue, mcpAnswering, selectServer, settle, template } from './harness.mjs';
import { AFRICA, ANIMALS_T, BODIES, CONGO, CONTINENTS, LISTINGS, ZOO, uri } from './zoo.mjs';

describe('putting a pick in a form', () => {
  let ui;
  let document;

  before(async () => {
    ({ ui, document } = await boot());
  });

  beforeEach(() => {
    ui.vocabularies.clear();
    ui.picks.clear();
    selectServer(ui, ZOO, LISTINGS);
    mcpAnswering(ui, BODIES);
    // A detail panel with no fields in it, so each test starts with no form open. Opened
    // through `openItem` rather than by emptying the node, because that is what leaving a
    // form does in the page: the send button deregisters, the picks are re-applied.
    ui.openItem('resources', { uri: uri('zoo://ticks'), name: `${ZOO}__zoo://ticks` });
  });

  /** The zoo's deepest template, whose form names both `continent` and `country`. */
  const animalsTemplate = () => template(ZOO, ANIMALS_T);

  /** Open that template's form. This is what `openItem` does on a click in the left column. */
  const openForm = () => ui.openItem('templates', animalsTemplate());

  /**
   * A tool whose `continent` argument is schema-constrained, so its field is a select.
   *
   * Two things about a form meet here: the column offers values, and a schema `enum`'s
   * options carry their *index* -- see `putValue` and python-mcp-gateway-8df.
   */
  const openTool = () => {
    const tool = {
      name: `${ZOO}__spot`,
      inputSchema: {
        type: 'object',
        properties: {
          continent: { type: 'string', enum: ['africa', 'asia'] },
          size: { type: 'integer', enum: [1, 3, 5] },
          count: { type: 'integer' },
        },
      },
    };
    ui.state.listings.tools = [tool];
    ui.openItem('tools', tool);
    return tool;
  };

  /** What a select-backed field is showing, in the spelling a person sees. */
  const chosen = (doc, name) => {
    const select = doc.querySelector(`#detail [data-field="${name}"] select`);
    return select.selectedOptions[0]?.dataset.value ?? select.value;
  };

  /** One pick, without going near the column's own rendering. */
  const set = (local, variable, values, { multi = false } = {}) => {
    const pick = ui.pickFor(uri(local), variable);
    pick.multi = multi;
    pick.values = new Set(values);
    return pick;
  };

  /** Walk the real cascade: open the root, pick a continent, pick a country. */
  const cascade = async () => {
    ui.openVocabulary(uri(CONTINENTS));
    await settle(ui);
    set(CONTINENTS, 'continent', ['africa']);
    await ui.refreshVariables();
    set(AFRICA, 'country', ['congo']);
    await ui.refreshVariables();
  };

  describe('a value picked into a form that is already open', () => {
    it('lands in the field that carries its name', async () => {
      await cascade();
      openForm();
      // Re-picking is what a click does, and it goes where the name says.
      ui.fillPick(set(CONTINENTS, 'continent', ['asia']));
      assert.equal(fieldValue(document, 'continent'), 'asia');
    });

    it('arrives the way a keystroke would, so the form recomputes', async () => {
      openForm();
      const seen = [];
      document.querySelector('#detail [data-field="continent"] input')
        .addEventListener('input', () => seen.push('input'));
      ui.fillField('continent', 'africa');
      assert.deepEqual(seen, ['input']);
      // The template's own expansion line is the proof that it recomputed.
      assert.match(document.querySelector('#detail .expansions').textContent, /africa/);
    });

    it('finds a field spelt differently, and one whose name merely ends in it', async () => {
      ui.state.listings.tools = [{
        name: `${ZOO}__spot`,
        inputSchema: { type: 'object', properties: { animalId: { type: 'string' } } },
      }];
      ui.openItem('tools', ui.state.listings.tools[0]);
      ui.fillField('id', 'okapi');
      assert.equal(fieldValue(document, 'animalId'), 'okapi');
    });

    it('says so when there is a field and the value cannot go in it', () => {
      const select = document.createElement('select');
      select.append(document.createElement('option'));
      assert.match(ui.putValue(select, 'congo'), /not one of the choices/);
      const box = document.createElement('input');
      box.type = 'checkbox';
      assert.match(ui.putValue(box, 'yes'), /checkbox/);
    });

    it('says nothing when there is simply nowhere to put it', async () => {
      // Picking before opening the thing that spends the value is how the column is meant
      // to be used, so a line of complaint every time would be the column talking over the
      // work. `#vars-note` stays hidden.
      openForm();
      ui.fillField('nosuchvariable', 'x');
      assert.equal(document.getElementById('vars-note').textContent, '');
      assert.ok(document.getElementById('vars-note').hidden);
    });
  });

  describe('a form opened onto picks already made', () => {
    it('fills every field the picks name, single values included', async () => {
      // The regression. Both picks exist before the form does, and neither is a fan-out.
      await cascade();
      assert.equal(fieldValue(document, 'continent'), undefined, 'no form yet');
      openForm();
      assert.equal(fieldValue(document, 'continent'), 'africa');
      assert.equal(fieldValue(document, 'country'), 'congo');
    });

    it('fills the chain again when another form is opened on top', async () => {
      await cascade();
      openForm();
      ui.openItem('resources', { uri: uri('zoo://ticks'), name: `${ZOO}__zoo://ticks` });
      assert.equal(fieldValue(document, 'continent'), undefined);
      openForm();
      assert.equal(fieldValue(document, 'continent'), 'africa');
    });

    it('scatters nothing into fields that do not carry the name', async () => {
      await cascade();
      // `id` is a real pick in the column -- the animals group's -- and this template names
      // no `id`. Nothing may take it on the grounds of being nearby.
      set(CONTINENTS, 'continent', []);
      set(AFRICA, 'country', []);
      ui.pickFor(uri(CONGO), 'id').values = new Set(['okapi']);
      openForm();
      assert.equal(fieldValue(document, 'continent'), '');
      assert.equal(fieldValue(document, 'country'), '');
    });

    it('will not fall back to the field you were last typing in', async () => {
      await cascade();
      openForm();
      const input = document.querySelector('#detail [data-field="country"] input');
      input.dispatchEvent(new document.defaultView.FocusEvent('focusin', { bubbles: true }));
      // Loosely, the last-focused field is a fair guess for a value with no home...
      ui.fillField('nosuchvariable', 'guess');
      assert.equal(fieldValue(document, 'country'), 'guess');
      // ...and strictly it is not, because a set written there would be *sent* literally.
      ui.fillPick(set('zoo://nowhere', 'nosuchvariable', ['a', 'b'], { multi: true }));
      assert.equal(fieldValue(document, 'country'), 'guess');
    });

    it('leaves a pick that has no values alone', async () => {
      await cascade();
      set(CONTINENTS, 'continent', []);
      openForm();
      assert.equal(fieldValue(document, 'continent'), '');
      assert.equal(fieldValue(document, 'country'), 'congo');
    });
  });

  describe('a pick of several values', () => {
    it('writes the set into the field, and marks it as holding one', async () => {
      await cascade();
      openForm();
      ui.fillPick(set(CONTINENTS, 'continent', ['africa', 'asia'], { multi: true }));
      assert.equal(fieldValue(document, 'continent'), '[africa, asia]');
      const field = document.querySelector('#detail [data-field="continent"]');
      assert.ok(field.classList.contains('field-fanned'));
    });

    it('is what the template expands over, a line per value', async () => {
      await cascade();
      openForm();
      ui.fillPick(set(CONTINENTS, 'continent', ['africa', 'asia'], { multi: true }));
      const lines = document.querySelector('#detail .expansions').textContent.split('\n');
      assert.equal(lines.length, 2);
      assert.ok(lines[0].includes('africa') && lines[1].includes('asia'));
      assert.ok(!lines.join().includes('['), 'no bracket reaches a URI');
    });

    it('is what the fan-out crosses, once per combination', async () => {
      await cascade();
      openForm();
      ui.fillPick(set(CONTINENTS, 'continent', ['africa', 'asia'], { multi: true }));
      ui.fillPick(set(AFRICA, 'country', ['congo', 'kenya'], { multi: true }));
      const fanned = ui.fannedFields();
      assert.deepEqual([...fanned.keys()].sort(), ['continent', 'country']);
      assert.equal(ui.spread({ continent: '[africa, asia]', country: '[congo, kenya]' }, fanned).length, 4);
    });

    it('keeps a number a number when it is crossed', () => {
      const fanned = new Map([['count', ['1', '2']]]);
      assert.deepEqual(ui.spread({ count: 7 }, fanned), [{ count: 1 }, { count: 2 }]);
      assert.deepEqual(ui.spread({ count: '7' }, fanned), [{ count: '1' }, { count: '2' }]);
    });

    it('shows only the first value in a field that would silently drop a set', () => {
      // A select takes one of its own options and a number input parses what it is given.
      // Neither can hold `[a, b]`, and a browser drops the text without saying so.
      openTool();
      ui.fillPick(set(CONTINENTS, 'continent', ['asia', 'africa'], { multi: true }));
      assert.equal(chosen(document, 'continent'), 'asia');
      assert.ok(!document.querySelector('#detail [data-field="continent"]')
        .classList.contains('field-fanned'));
      ui.fillPick(set('zoo://sizes', 'count', ['3', '4'], { multi: true }));
      assert.equal(fieldValue(document, 'count'), '3');
    });
  });

  describe('taking a value back out', () => {
    it('empties the field when the pick is buried', async () => {
      await cascade();
      openForm();
      assert.equal(fieldValue(document, 'country'), 'congo');
      // Asia has no Congo, so the country pick is no longer a claim about anything.
      set(CONTINENTS, 'continent', ['asia']);
      await ui.refreshVariables();
      assert.ok(!ui.picks.has(uri(AFRICA)));
      assert.equal(fieldValue(document, 'country'), '');
    });

    it('empties it the way a keystroke would, so the form stops claiming the value', async () => {
      await cascade();
      openForm();
      const seen = [];
      document.querySelector('#detail [data-field="country"] input')
        .addEventListener('input', () => seen.push('input'));
      ui.buryField({ variable: 'country', values: new Set(['congo']) });
      assert.deepEqual(seen, ['input']);
      assert.doesNotMatch(document.querySelector('#detail .expansions').textContent, /congo/);
    });

    it('leaves a value the field no longer agrees with alone', async () => {
      // Typed over since it was picked: that is the person's value now, not the column's.
      await cascade();
      openForm();
      const input = document.querySelector('#detail [data-field="country"] input');
      input.value = 'kenya';
      ui.buryField({ variable: 'country', values: new Set(['congo']) });
      assert.equal(input.value, 'kenya');
    });

    it('empties every field when the vocabulary is closed', async () => {
      await cascade();
      openForm();
      ui.closeVocabulary(uri(CONTINENTS));
      assert.equal(ui.picks.size, 0);
      assert.equal(fieldValue(document, 'continent'), '');
      assert.equal(fieldValue(document, 'country'), '');
    });
  });
});
