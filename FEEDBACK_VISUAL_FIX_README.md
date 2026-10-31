# Feedback visual fix

This patch fixes the generated report page rendering the full Billing AI Assistant instead of the Satisfied/Unsatisfied feedback panel.

## Why it happened
The generated custom visual had feedback properties but no data query/data-role binding. Power BI could omit `dataViews`, so `visual.ts` could not read the feedback mode and rendered the normal chatbot.

## Files changed
- `billingChatbot/capabilities.json`: adds an internal `feedbackAnchor` grouping role and table data-view mapping.
- `nl2sql_poc/nl2sql_poc/app/powerbi_project_generator.py`: binds the first report column to that role on the feedback-only visual, while retaining the request/session properties.
- `nl2sql_poc/nl2sql_poc/tests/test_powerbi_generator.py`: asserts that the anchor binding is generated.
- `REPORT_PAGE_FEEDBACK_CHANGE.md`: documents the behavior.

## Apply and test on Windows
1. Back up your current `newest` folder.
2. Copy the updated files from this archive into the matching paths in your existing `newest` folder, overwriting those files only.
3. In `billingChatbot`, run `npm run package`.
4. Import the newly generated `.pbiviz` from `billingChatbot\dist` into Power BI Desktop.
5. Restart the Python backend so it loads the updated generator code.
6. Generate a **new report**. Existing pages are not retroactively changed.
7. Confirm the generated report page shows Satisfied/Unsatisfied and not the full chatbot.

The generator tests passed (4 tests). The full test suite could not be collected here because `test_groq_connection.py` exits when `GROQ_API_KEY` is not set. The package build should be run on the user's Windows environment with their installed `pbiviz` CLI.
