// Tests the ACTUAL app code (handleQuery brain + fmt renderer) extracted from HR_Assistant.html — no browser needed.
const fs = require("fs");
let s = fs.readFileSync("HR_Assistant.html", "utf8");
s = s.substring(s.indexOf("<script>") + 8);
s = s.substring(0, s.indexOf("const log=document.getElementById")); // stop before DOM code
const { handleQuery, fmt } = (new Function(s + "\n;return {handleQuery, fmt};"))();

const CASES = [
  {q:"Get me the details for Jane Doe", tools:["search_workers","get_worker"], want:["Jane Doe","Senior Software Engineer","jane.doe@example.com","Michael Chen"]},
  {q:"Who does Jane Doe report to?", tools:["search_workers","get_assignment"], want:["reports to","Michael Chen"]},
  {q:"Who reports to Priya Nair?", tools:["search_workers","get_direct_reports"], want:["manages 2 people","Michael Chen","Ahmed Khan"]},
  {q:"Reporting chain above Jane Doe", tools:["search_workers","get_management_chain"], want:["Michael Chen","Priya Nair","Robert King"]},
  {q:"Who works with Sara Williams?", tools:["search_workers","get_assignment","list_by_department"], want:["Human Resources","Linda Gomez"]},
  {q:"How many people are in Engineering?", tools:["list_by_department"], want:["3","Engineering"]},
  {q:"Show me everyone in Finance", tools:["list_by_department"], want:["David Smith","Emily Turner"]},
  {q:"Find someone called Bob Marley", tools:["search_workers"], want:["find","Bob Marley"]},
];

let pass = 0;
console.log("── Query brain (intent → MCP tools → answer) ──");
for (const c of CASES) {
  const r = handleQuery(c.q);
  const tools = [...new Set(r.used)];
  const miss = [...c.want.filter(f => !r.answer.toLowerCase().includes(f.toLowerCase())),
                ...c.tools.filter(t => !tools.includes(t))];
  const ok = miss.length === 0;
  console.log((ok ? "PASS ✅" : "FAIL ❌") + "  " + c.q);
  console.log("        tools: " + tools.join(" → "));
  console.log("        → " + r.answer.replace(/\n/g, " ").slice(0, 95));
  if (!ok) console.log("        MISSING: " + miss.join(", "));
  if (ok) pass++;
}

console.log("\n── HTML renderer (fmt) ──");
const rt = [
  ["bold", fmt("**Jane Doe**").includes("<strong>Jane Doe</strong>")],
  ["bullets", fmt("• a").includes('<div class="li">')],
  ["HTML-escaping (XSS safe)", fmt("<script>x</script>").includes("&lt;script&gt;") && !fmt("<script>").includes("<script>")],
];
let rp = 0;
for (const [n, ok] of rt) { console.log((ok ? "PASS ✅" : "FAIL ❌") + "  " + n); if (ok) rp++; }

console.log("\n================  RESULT: " + (pass + rp) + "/" + (CASES.length + rt.length) + " checks passed  ================");
process.exit((pass === CASES.length && rp === rt.length) ? 0 : 1);
