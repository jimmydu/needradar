// Usage: node charts.mjs <collection> <category> <num>
// collection: GROSSING | TOP_FREE | TOP_PAID; category: e.g. HEALTH_AND_FITNESS or 'APPLICATION'
import gplay from "google-play-scraper";
const [collection, category, num] = process.argv.slice(2);
const res = await gplay.list({
  collection: gplay.collection[collection],
  category: category === "APPLICATION" ? undefined : gplay.category[category],
  num: parseInt(num || "100", 10),
  country: "us",
  lang: "en",
});
console.log(JSON.stringify(res.map((a, i) => ({
  rank: i + 1, appId: a.appId, title: a.title, developer: a.developer,
  developerId: a.developerId, score: a.score, ratings: a.ratings,
  price: a.price, free: a.free, genre: a.genre, icon: a.icon,
}))));
