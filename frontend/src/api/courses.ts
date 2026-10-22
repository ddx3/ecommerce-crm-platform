import client from './client'
import type {
  CourseListItem,
  CourseDetail,
  LessonContent,
  MyCoursesResponse,
  PaginatedResponse,
} from '../types'

export interface CoursesParams {
  category?: string
  page?: number
  page_size?: number
}

export const getCourses = (params?: CoursesParams) =>
  client.get<PaginatedResponse<CourseListItem>>('/courses/', { params }).then(r => r.data)

export const getCourse = (slug: string) =>
  client.get<CourseDetail>(`/courses/${slug}/`).then(r => r.data)

export const getLesson = (slug: string, lessonId: number) =>
  client.get<LessonContent>(`/courses/${slug}/lessons/${lessonId}/`).then(r => r.data)

export const getMyCourses = () =>
  client.get<MyCoursesResponse>('/me/courses/').then(r => r.data)

export const getProductCourses = (productSlug: string) =>
  client.get<CourseListItem[]>(`/products/${productSlug}/courses/`).then(r => r.data)
