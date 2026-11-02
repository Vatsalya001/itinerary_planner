[out:json][timeout:90];
(
  nwr["amenity"~"^(cafe|restaurant|ice_cream|fast_food)$"]["name"]["diet:vegetarian"](around:6000,12.9716,77.5946);
  nwr["amenity"~"^(cafe|restaurant|ice_cream|fast_food)$"]["name"]["diet:vegan"](around:6000,12.9716,77.5946);
  nwr["leisure"~"^(park|garden)$"]["name"]["wikidata"](around:6000,12.9716,77.5946);
  nwr["tourism"~"^(museum|gallery)$"]["name"](around:6000,12.9716,77.5946);
  nwr["shop"="books"]["name"](around:6000,12.9716,77.5946);
  nwr["amenity"~"^(theatre|arts_centre|music_venue|concert_hall)$"]["name"](around:6000,12.9716,77.5946);
  nwr["natural"="water"]["water"="lake"]["name"]["wikidata"](around:6000,12.9716,77.5946);
);
out center tags;
