import "@testing-library/jest-dom/vitest";

Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({
        matches: false,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
    }),
});

HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
};

HTMLDialogElement.prototype.close = function close() {
    this.open = false;
};
