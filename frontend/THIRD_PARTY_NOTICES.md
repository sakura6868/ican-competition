# Frontend third-party notices

Updated: 2026-10-14.

## Sakai Vue

Source: https://github.com/primefaces/sakai-vue (master).
Copyright (c) 2018-2022 PrimeTek. MIT license; full text: LICENSES/sakai-MIT.txt.

Adapted src/layout/AppLayout.vue and src/layout/composables/layout.js into
src/layouts/MainLayout.vue and src/layouts/useEducationLayout.ts.
Changes: TypeScript state, education navigation, role permissions, responsive focus handling,
local brand, no PrimeVue configuration or vendor API dependencies.

## Frappe Learning

Source: https://github.com/frappe/lms (develop).
License: GNU Affero General Public License v3; full text: LICENSES/frappe-lms-AGPL-3.0.txt.

Adapted frontend/src/components/CourseCard.vue into src/components/LearningCourseCard.vue.
Retained the cover/content/lesson-count/progress/footer structure and clamped introduction.
Changes: SOP course props, local CSS, real session progress, original abstract cover artwork;
removed Frappe resources, ratings, enrollment counts and instructor dependencies.
The surrounding project already uses AGPL-3.0. These changes do not change its license.

Upstream app images, logos, databases, sample enrollments and backend APIs were not imported.
No runtime CDN dependencies were added. Source snapshots and retrieval hashes are recorded below.

- `sakai-vue/src/layout/AppLayout.vue`: SHA-256 `14f4550c753cf4e382a0e4fada65120ea3579e57ae27f4164dbe7d7d464e3923`

- `sakai-vue/src/layout/composables/layout.js`: SHA-256 `7bdb0bab6bbe8abd41acaf49a7b68236eec34636493b5b429d8c4d8ae87d557e`

- `frappe-lms/frontend/src/components/CourseCard.vue`: SHA-256 `154bdc3460a1c0937282749e0993b56d728d116e40f5d6b7601e70f407512ac6`
