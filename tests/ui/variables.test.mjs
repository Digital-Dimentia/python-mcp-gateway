// The variables column's resolver: which listings pair, which groups a cascade produces,
// and which picks survive a change upstream.
//
// Nothing here draws anything. What is asserted is the shape the resolver settles on,
// because that is what the *next* link expands against -- a group that thought it was still
// `continent` would write the country into the continent's segment and read a URI nobody
// published, which is exactly the bug that started this directory.

import { describe, it, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import { boot, enumBody, mcpAnswering, selectServer, settle, shape } from './harness.mjs';

import {
  AFRICA, ANIMALS_T, ASIA, BODIES, CONGO, CONTINENTS, COUNTRIES_T, LISTINGS, ZOO, uri,
} from './zoo.mjs';

describe('the variables column', () => {
  let ui;
  let reads;

  before(async () => {
    ({ ui } = await boot());
  });

  beforeEach(() => {
    ui.vocabularies.clear();
    ui.picks.clear();
    selectServer(ui, ZOO, LISTINGS);
    reads = mcpAnswering(ui, BODIES);
  });

  /** Ask for a vocabulary, and wait out the read it starts. See `settle`. */
  const open = async (local) => {
    ui.openVocabulary(uri(local));
    await settle(ui);
  };

  /** Pick one value in one group, then let the column resolve what that opens up. */
  const pick = async (local, variable, value) => {
    ui.pickFor(uri(local), variable).values = new Set([value]);
    await ui.refreshVariables();
  };

  describe('pairing a listing with its template', () => {
    it('reads the pair off the URIs rather than guessing', () => {
      const pairs = ui.vocabularyPairs();
      assert.deepEqual(
        pairs.map((pair) => `${pair.variable}@${pair.listing}`).sort(),
        ['continent@zoo://continents', 'id@zoo://animals'],
      );
    });

    it('leaves a listing that pairs with no template alone', () => {
      // `zoo://ticks` exists to prove a read can move state, so a column that fetched it
      // speculatively would be changing the server by being looked at.
      const pairs = ui.vocabularyPairs();
      assert.ok(!pairs.some((pair) => pair.listing === 'zoo://ticks'));
    });

    it('keeps one group per listing when two templates share a prefix', () => {
      // Both templates cut back to `zoo://continents`, and `vocabularies` and `picks` are
      // keyed by the listing's URI -- so a second group on that key would alias the first
      // one's cache and its picks.
      const pairs = ui.vocabularyPairs().filter((pair) => pair.uri === uri(CONTINENTS));
      assert.equal(pairs.length, 1);
      assert.equal(pairs[0].template, COUNTRIES_T, 'the simplest template wins the pairing');
    });

    it('does not depend on the order the server published them', () => {
      const spell = (pairs) => pairs.map((pair) => `${pair.variable}@${pair.template}`).sort();
      const forwards = spell(ui.vocabularyPairs());
      selectServer(ui, ZOO, {
        resources: [...LISTINGS.resources].reverse(),
        templates: [...LISTINGS.templates].reverse(),
      });
      assert.deepEqual(spell(ui.vocabularyPairs()), forwards);
    });

    it('offers nothing for a server that publishes no pair', () => {
      selectServer(ui, ZOO, { resources: ['zoo://ticks'], templates: [] });
      assert.deepEqual(ui.vocabularyPairs(), []);
    });
  });

  describe('the groups on screen', () => {
    it('starts as a menu: nothing is a group, and nothing is read', async () => {
      await ui.refreshVariables();
      assert.deepEqual(ui.vocabularyGroups(), []);
      assert.deepEqual(reads, []);
    });

    it('reads only the vocabulary that was opened', async () => {
      await open(CONTINENTS);
      assert.deepEqual(reads, [uri(CONTINENTS)]);
      // The child is drawn from the first body's `narrows`, and is not read: see below.
      assert.deepEqual(shape(ui.vocabularyGroups()), [
        'continent@zoo://continents',
        '?@pending:pick',
      ]);
    });

    it('names a child for the first variable the chain has not bound', async () => {
      // The bug this file exists for. `zoo://continents/africa/countries` is spent on a
      // template naming `continent` *and* `country`; the continent is already in the URI,
      // so this group is the `country` one -- and a child that inherited its parent's
      // `continent` would expand the next link into `zoo://continents/congo/countries/`.
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      assert.deepEqual(shape(ui.vocabularyGroups()), [
        'continent@zoo://continents',
        'country@zoo://continents/africa/countries',
        // And the next link waits for a country, rather than being read under one.
        '?@pending:pick',
      ]);
    });

    it('follows the chain to the end, one level per resolve', async () => {
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      await pick(AFRICA, 'country', 'congo');
      assert.deepEqual(shape(ui.vocabularyGroups()), [
        'continent@zoo://continents',
        'country@zoo://continents/africa/countries',
        'id@zoo://continents/africa/countries/congo/animals',
      ]);
      // Each level is read exactly once, and only after the pick that named it.
      assert.deepEqual(reads, [uri(CONTINENTS), uri(AFRICA), uri(CONGO)]);
    });

    it('reads each listing at a URI something handed it, never one it invented', async () => {
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      await pick(AFRICA, 'country', 'congo');
      // `zoo://animals` pairs with a template and would be a perfectly good vocabulary --
      // and is still not read, because nobody opened it.
      assert.ok(!reads.includes(uri('zoo://animals')));
      for (const read of reads) assert.ok(read in BODIES, read);
    });

    it('draws a child as pending while its parent has no single pick', async () => {
      await open(CONTINENTS);
      const [, child] = ui.vocabularyGroups();
      assert.equal(child.pending, 'pick');
      assert.equal(child.from, 'continent', 'the group says which pick would fill it');
      assert.deepEqual(reads, [uri(CONTINENTS)], 'and a pending group is not read');
    });

    it('keeps a child pending while several values are picked in its parent', async () => {
      // A `narrows` buys one listing. Two continents' countries merged would be a
      // vocabulary the server never published.
      await open(CONTINENTS);
      const pick = ui.pickFor(uri(CONTINENTS), 'continent');
      pick.multi = true;
      pick.values = new Set(['africa', 'asia']);
      await ui.refreshVariables();
      assert.equal(ui.vocabularyGroups()[1].pending, 'pick');
      assert.deepEqual(reads, [uri(CONTINENTS)]);
    });

    it('stops rather than expanding a template with a segment still empty', async () => {
      // A listing whose `narrows` names two variables the chain cannot supply: expanding
      // now would read `…/countries//animals`, which is a different URI from the one meant.
      const bodies = {
        [uri(CONTINENTS)]: enumBody(['africa'], { narrows: ANIMALS_T }),
      };
      reads = mcpAnswering(ui, bodies);
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      const [, child] = ui.vocabularyGroups();
      assert.equal(child.pending, 'unbound');
      assert.equal(child.from, 'country');
      assert.deepEqual(reads, [uri(CONTINENTS)]);
    });

    it('refuses to spin on listings that point at each other in a circle', async () => {
      const bodies = { [uri(CONTINENTS)]: enumBody(['africa'], { narrows: CONTINENTS }) };
      reads = mcpAnswering(ui, bodies);
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      // Bounded by MAX_CHAIN_DEPTH, and the depths run 0..4 rather than forever.
      const depths = ui.vocabularyGroups().map((group) => group.depth);
      assert.deepEqual(depths, [0, 1, 2, 3, 4]);
    });
  });

  describe('a pick that no longer has a group', () => {
    it('is buried when the continent above it changes', async () => {
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      await pick(AFRICA, 'country', 'congo');
      assert.ok(ui.picks.has(uri(AFRICA)));

      await pick(CONTINENTS, 'continent', 'asia');
      assert.ok(!ui.picks.has(uri(AFRICA)), 'a country picked under Africa is not Asia\'s');
      assert.ok(!ui.picks.has(uri(CONGO)), 'and neither is the animal under it');
      assert.deepEqual(shape(ui.vocabularyGroups()), [
        'continent@zoo://continents',
        'country@zoo://continents/asia/countries',
        '?@pending:pick',
      ]);
    });

    it('keeps the bodies, so going back costs no second read', async () => {
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      await pick(CONTINENTS, 'continent', 'asia');
      await pick(CONTINENTS, 'continent', 'africa');
      assert.deepEqual(reads, [uri(CONTINENTS), uri(AFRICA), uri(ASIA)]);
    });

    it('is buried when its vocabulary is closed', async () => {
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      await pick(AFRICA, 'country', 'congo');

      ui.closeVocabulary(uri(CONTINENTS));
      assert.deepEqual(ui.vocabularyGroups(), []);
      assert.equal(ui.picks.size, 0);
      assert.deepEqual(ui.livePicks(), []);
    });

    it('is out of the fan-out the moment its group goes, not at the next resolve', async () => {
      // `livePicks` is what keeps a pick made a moment before a group closed out of a send
      // that happens a moment after: the fan-out reads `picks` directly.
      await open(CONTINENTS);
      await pick(CONTINENTS, 'continent', 'africa');
      const ghost = uri('zoo://nowhere');
      ui.picks.set(ghost, { variable: 'ghost', multi: false, values: new Set(['x']) });
      assert.ok(ui.picks.has(ghost));
      assert.ok(!ui.livePicks().some(([key]) => key === ghost));
      // The groups that *are* drawn keep theirs, pick or no pick: drawing a group is what
      // creates its pick state.
      assert.deepEqual(ui.livePicks().map(([key]) => key).sort(), [uri(AFRICA), uri(CONTINENTS)].sort());
    });
  });

  describe('reading values out of a body', () => {
    it('takes a Schema enum fragment, with labels and where its values go', () => {
      const read = ui.valuesFrom({
        enum: ['okapi', 'tapir'],
        enumNames: ['Okapi', 'tapir'],
        readOne: 'zoo://animals/{id}',
      });
      assert.deepEqual(read.values, [
        { value: 'okapi', label: 'Okapi' },
        // A label that says nothing the value does not is dropped rather than repeated.
        { value: 'tapir', label: null },
      ]);
      assert.equal(read.readOne, 'zoo://animals/{id}');
      assert.equal(read.narrows, null);
    });

    it('takes an array of scalars, and of records with an id', () => {
      assert.deepEqual(ui.valuesFrom([1, 'two']).values, [
        { value: '1', label: null }, { value: 'two', label: null },
      ]);
      assert.deepEqual(ui.valuesFrom([{ id: 'a', title: 'A' }, { uri: 'b' }, null, {}]).values, [
        { value: 'a', label: 'A' }, { value: 'b', label: null },
      ]);
    });

    it('takes an object keyed by the identifier', () => {
      assert.deepEqual(ui.valuesFrom({ okapi: { name: 'Okapi' }, tapir: null }).values, [
        { value: 'okapi', label: 'Okapi' }, { value: 'tapir', label: null },
      ]);
    });

    it('says so rather than inventing values, when the body is not a set', () => {
      for (const body of [42, 'prose', true]) {
        const read = ui.valuesFrom(body);
        assert.deepEqual(read.values, []);
        assert.match(read.error, /not a set of values|single JSON scalar/);
      }
    });

    it('will not split prose into a list of garbage', () => {
      const read = ui.readVocabulary({ contents: [{ text: 'The zoo has\nmany animals.' }] });
      assert.deepEqual(read.values, []);
      assert.match(read.error, /not JSON/);
    });

    it('says so when there is no text to read at all', () => {
      const read = ui.readVocabulary({ contents: [{ blob: 'AAAA' }] });
      assert.deepEqual(read.values, []);
      assert.match(read.error, /no text content/);
    });
  });
});
