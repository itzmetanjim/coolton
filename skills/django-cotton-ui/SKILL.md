---
name: django-cotton-ui
description: 'Builds Django interfaces with Django Cotton UI components, using accessible, themeable Cotton components with Alpine.js and Tailwind CSS. USE FOR: implementing or reviewing Django Cotton UI components. DO NOT USE FOR: generic Django templates without Cotton.'
---

# Django Cotton Ui

# django-cotton ui

## when to use
Use this skill when building, reviewing, or explaining interfaces made with the Django Cotton UI kit: accessible, themeable Django components built on Cotton with Alpine.js and Tailwind CSS.

## reference
- Documentation and component gallery: https://django-cotton.com/ui
- Core Django Cotton documentation: https://django-cotton.com/

## workflow
1. Check the UI documentation for the closest component and its current markup/API before writing code.
2. Prefer the documented Cotton component patterns over hand-rolled HTML.
3. Preserve accessibility: labels, keyboard interaction, focus states, semantic controls, and useful validation/error text.
4. Keep styling themeable; use the documented Tailwind/Cotton conventions rather than hard-coding conflicting styles.
5. Use Alpine.js only for the interactive behavior required by the component, and keep server-side Django behavior explicit.
6. If the requested component is not documented, say so and compose it from documented primitives instead of inventing a component API.
7. Include any required setup, imports, context variables, and asset/configuration changes in the implementation.

## output
Return the relevant Django template/component markup, supporting Python or JavaScript only when needed, and a short note describing required setup and accessibility behavior.

## constraints
Do not claim an API exists without checking the documentation. Do not add dependencies or alter production configuration without explicit approval.
