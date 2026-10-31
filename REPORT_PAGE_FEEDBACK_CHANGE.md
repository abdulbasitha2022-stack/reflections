# Report-page feedback controls

The generated PBIR report page now includes a dedicated instance of the `BillingChatbot` custom visual in feedback-only mode when a `request_id` is supplied. It is positioned in a reserved strip at the bottom of the report page and receives the report's `request_id` and `session_id` as visual properties.

- `Satisfied` and `Unsatisfied` submit to `POST http://localhost:5056/feedback` with the report-specific `request_id`.
- After a successful rating, the buttons are disabled and a confirmation is shown.
- An Unsatisfied rating displays the return-to-conversation and finish-session choices. The return action instructs the user to navigate to the assistant page, preserving the same session; Power BI's page navigation is not controlled directly by this custom visual.
- `Finish session` calls `POST /session/<session_id>/end`.

## Rebuild and verify

1. From `billingChatbot`, run `npm run package` and import the newly built `.pbiviz` into Power BI Desktop (replace the old visual if prompted).
2. Restart/reload the Power BI project and generate a fresh report. Existing report pages do not gain the new visual retroactively; generate them again or add the visual manually.
3. Confirm that the generated report page displays the feedback panel, then test both ratings and check `report_request_metadata.customer_satisfaction` for the matching `request_id`.

## Validation performed in this environment

- TypeScript type-check: passed.
- ESLint for `src/visual.ts`: passed.
- Python compilation: passed.
- `tests/test_powerbi_generator.py`: 4 passed, including a check that a report page includes the feedback visual with the correct request and session IDs.
- The full Python suite could not be collected because Flask is not installed in this environment. Power BI Desktop runtime behavior and the generated PBIR custom visual configuration still need verification on the user's machine.
