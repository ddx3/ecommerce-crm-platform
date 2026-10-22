import client from './client'
import type {
  BlogPostListItem,
  BlogPostDetail,
  PaginatedResponse,
} from '../types'

export interface BlogListParams {
  page?: number
  page_size?: number
  limit?: number
}

export const getBlogPosts = (params?: BlogListParams) =>
  client
    .get<PaginatedResponse<BlogPostListItem>>('/blog/posts/', { params })
    .then(r => r.data)

export const getBlogPost = (slug: string) =>
  client.get<BlogPostDetail>(`/blog/posts/${slug}/`).then(r => r.data)
