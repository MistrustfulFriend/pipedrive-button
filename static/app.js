const out = document.getElementById("out");
document.getElementById("btn").addEventListener("click", async () => {
  out.textContent = "Calling /health ...";
  const res = await fetch("/health");
  const data = await res.json();
  out.textContent = JSON.stringify(data, null, 2);
});
