// Counts in short form: 999, 1.2k, 45k, 2.3L (lakh), 1.5Cr (crore).
(function () {
  const units = [[1e7, "Cr"], [1e5, "L"], [1e3, "k"]];
  function shortCount(value) {
    const number = Math.max(0, Math.floor(Number(value) || 0));
    for (const [size, unit] of units) {
      if (number >= size) {
        const scaled = number / size;
        return (scaled >= 10 ? Math.floor(scaled) : Math.floor(scaled * 10) / 10) + unit;
      }
    }
    return String(number);
  }
  if (typeof module !== "undefined") module.exports = shortCount;
  if (typeof window !== "undefined") window.shortCount = shortCount;
})();
