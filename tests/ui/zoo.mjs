// One zoo, shared by both suites.
//
// The shape is `examples/zoo_server.py`'s: a continent narrows to a country, a country
// narrows to its animals, and an animal is read one at a time. It is here rather than in
// either test file because the two suites assert about the same cascade from opposite ends
// -- what the resolver makes of it, and what it puts in a form -- and two copies of the
// table would drift.

import { enumBody, gatewayUri } from './harness.mjs';

export const ZOO = 'zoo';

/** A backend URI in the gateway's address space. */
export const uri = (local) => gatewayUri(ZOO, local);

export const CONTINENTS = 'zoo://continents';
export const COUNTRIES_T = 'zoo://continents/{continent}/countries';
export const ANIMALS_T = 'zoo://continents/{continent}/countries/{country}/animals';
export const ANIMAL_T = 'zoo://animals/{id}';
export const AFRICA = 'zoo://continents/africa/countries';
export const ASIA = 'zoo://continents/asia/countries';
export const CONGO = 'zoo://continents/africa/countries/congo/animals';

/** The listings and templates, in the order a server happens to publish them. */
export const LISTINGS = {
  //: `zoo://ticks` pairs with no template and must therefore never be read: it exists in
  //: the fixture server precisely to prove that a read can move state.
  resources: [CONTINENTS, 'zoo://animals', 'zoo://ticks'],
  templates: [COUNTRIES_T, ANIMALS_T, ANIMAL_T],
};

/** What `resources/read` answers, keyed by the gateway's URI. */
export const BODIES = {
  [uri(CONTINENTS)]: enumBody(['africa', 'asia'], { narrows: COUNTRIES_T }),
  [uri(AFRICA)]: enumBody(['congo', 'kenya'], { narrows: ANIMALS_T }),
  [uri(ASIA)]: enumBody(['nepal'], { narrows: ANIMALS_T }),
  [uri(CONGO)]: enumBody(['okapi'], { readOne: ANIMAL_T }),
  [uri('zoo://animals')]: enumBody(['okapi', 'tapir'], { readOne: ANIMAL_T }),
};
