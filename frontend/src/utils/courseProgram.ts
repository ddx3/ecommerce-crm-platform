import type { CourseModule, CourseLessonProgram } from '../types'

/** The course curriculum is a tree of identical nodes: a node has one title, and it can itself
 * be a lesson (video/text) even if it contains subsections.
 * A lesson does not live in the DB without a module, so the node's own lesson arrives as a separate
 * field
 * own_lesson and is NOT duplicated in the lessons list.
 */

export type ProgramEntry =
  | { kind: 'lesson'; num: string; lesson: CourseLessonProgram }
  | { kind: 'section'; num: string; module: CourseModule }

export function ownLesson(m: CourseModule): CourseLessonProgram | null {
  return m.own_lesson ?? null
}

/** A node without subsections and without other lessons is just a lesson in a single row. */
export function isLeafNode(m: CourseModule): boolean {
  return !!m.is_leaf && !!ownLesson(m)
}

/** Lessons and subsections share one sort_order within the parent, so they can
 * interleave. On a tie (old structure with separate counters) lessons come
 * first — same as in the CRM.
 */
export function programItems(m: CourseModule, num: string): ProgramEntry[] {
  const raw: { order: number; lesson?: CourseLessonProgram; module?: CourseModule }[] = [
    ...m.lessons.map(l => ({ order: l.sort_order, lesson: l })),
    ...(m.children ?? []).map(c => ({ order: c.sort_order, module: c })),
  ]
  raw.sort((a, b) => a.order - b.order || (a.lesson ? -1 : 1))
  return raw.map((it, i) =>
    it.lesson
      ? { kind: 'lesson' as const, num: `${num}.${i + 1}`, lesson: it.lesson }
      : { kind: 'section' as const, num: `${num}.${i + 1}`, module: it.module! },
  )
}

/** How many lessons are in the subtree — for the "N lessons" label. */
export function lessonCount(m: CourseModule): number {
  if (isLeafNode(m)) return 1
  return (ownLesson(m) ? 1 : 0) + m.lessons.length
    + (m.children ?? []).reduce((sum, c) => sum + lessonCount(c), 0)
}

/** All course lessons in curriculum order — for "next/back" navigation. */
export function flattenLessons(modules: CourseModule[]): CourseLessonProgram[] {
  const out: CourseLessonProgram[] = []
  const walk = (m: CourseModule) => {
    const own = ownLesson(m)
    if (own) out.push(own)
    if (isLeafNode(m)) return
    programItems(m, '').forEach(it => {
      if (it.kind === 'lesson') out.push(it.lesson)
      else walk(it.module)
    })
  }
  modules.forEach(walk)
  return out
}

/** A node with nothing visible left (all lessons and subsections hidden)
 * is not shown on the storefront — otherwise an empty accordion "0 lessons" would hang
 * there.
 */
export function hasVisibleContent(m: CourseModule): boolean {
  if (ownLesson(m)) return true
  if (m.lessons.length) return true
  return (m.children ?? []).some(hasVisibleContent)
}
