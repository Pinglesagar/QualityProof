import tseslint from "typescript-eslint";

/**
 * The locator doctrine, enforced mechanically.
 *
 * A convention that lives only in a style guide erodes. Making brittle
 * selectors a lint error is what keeps "prefer role-based locators" true a year
 * later, and it mirrors the preference order the Python generator applies.
 */
export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**", "src/generated/**", "test-results/**"] },
  ...tseslint.configs.recommended,
  {
    files: ["example/**/*.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "CallExpression[callee.property.name='locator'] > Literal[value=/^[#.]/]",
          message:
            "Use a role, label, placeholder or test-id locator: a CSS hook breaks when markup is restyled, which is the exact defect class this project measures.",
        },
        {
          selector: "CallExpression[callee.property.name='waitForTimeout']",
          message:
            "Fixed sleeps are flaky by construction. Assert on a web-first expectation instead.",
        },
      ],
    },
  },
  {
    files: ["src/**/*.ts"],
    rules: { "@typescript-eslint/no-explicit-any": "error" },
  },
);
