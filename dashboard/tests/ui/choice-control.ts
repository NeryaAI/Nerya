import { expect, type Locator } from "@playwright/test";

/** Interact with the visible list rather than changing hidden form values. */
export async function chooseOption(control: Locator, choice: string | { index: number }) {
  if (await control.evaluate((node) => node.tagName === "SELECT")) {
    await control.selectOption(choice);
    return;
  }
  await control.click();
  const list = control.page().getByRole("listbox");
  await expect(list).toBeVisible();
  const options = list.getByRole("option");
  const index = typeof choice === "string"
    ? await options.evaluateAll((nodes, value) => nodes.findIndex((node) => node.getAttribute("data-value") === value), choice)
    : choice.index;
  expect(index, "Requested option must exist").toBeGreaterThanOrEqual(0);
  await options.nth(index).click();
  await expect(list).toBeHidden();
}
