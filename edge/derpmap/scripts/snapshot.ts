import { writeFile } from "node:fs/promises";

const UPSTREAM = "https://tailcat.dev/derpmap.json";
const response = await fetch(UPSTREAM);
if (!response.ok) throw new Error(`upstream ${UPSTREAM} -> ${response.status}`);
const text = await response.text();
JSON.parse(text);
await writeFile("src/snapshot.json", text.endsWith("\n") ? text : `${text}\n`);
console.log("snapshot refreshed from", UPSTREAM);
