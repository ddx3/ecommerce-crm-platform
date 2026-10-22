import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, Calendar } from 'lucide-react'
import { getBlogPosts, type BlogListParams } from '../../api/blog'
import type { BlogPostListItem, PaginatedResponse } from '../../types'
import Seo from '../../components/Seo'

export function formatBlogDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString('uk-UA', { day: 'numeric', month: 'long', year: 'numeric' })
}

export function BlogCard({ post }: { post: BlogPostListItem }) {
  return (
    <Link
      to={`/blog/${post.slug}`}
      className="group block h-full bg-surface border border-border rounded-2xl shadow-card-low overflow-hidden hover:shadow-card-hover flex flex-col"
    >
      <div className="aspect-video bg-surface-muted overflow-hidden">
        {post.preview_image ? (
          <img src={post.preview_image} alt={post.title} loading="lazy" decoding="async" className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-gray-200">
            <Calendar size={40} />
          </div>
        )}
      </div>
      <div className="px-4 pt-3 pb-4 flex flex-col flex-1">
        <h3 className="text-base font-bold leading-snug text-text-primary line-clamp-2 min-h-[2lh]">{post.title}</h3>
        <p className="text-xs text-text-muted mt-auto pt-2">{formatBlogDate(post.created_at)}</p>
      </div>
    </Link>
  )
}

const PAGE_SIZE = 12

export default function BlogPage() {
  const [data, setData] = useState<PaginatedResponse<BlogPostListItem> | null>(null)
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [showAll, setShowAll] = useState(false)

  useEffect(() => {
    setLoading(true)
    const params: BlogListParams = showAll ? { limit: 500 } : { page, page_size: PAGE_SIZE }
    getBlogPosts(params)
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [page, showAll])

  const totalPages = data ? Math.max(1, Math.ceil(data.count / PAGE_SIZE)) : 1

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <Seo
        title="Blog"
        description="Tips on caring for plants, the garden and the vegetable patch from “Magiya Roslyn”."
        canonicalPath="/blog"
      />
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600">Blog</span>
      </nav>

      <h1 className="text-2xl sm:text-3xl font-bold text-gray-900 mb-6 sm:mb-8">Blog</h1>

      {loading && !data && (
        <div className="min-h-[40vh] flex items-center justify-center">
          <div className="w-10 h-10 border-4 border-gray-200 border-t-brand rounded-full animate-spin" />
        </div>
      )}

      {!loading && data && data.results.length === 0 && (
        <p className="text-gray-500 text-base">No articles yet.</p>
      )}

      {data && data.results.length > 0 && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 sm:gap-4">
            {data.results.map(post => <BlogCard key={post.id} post={post} />)}
          </div>

          {showAll && data.count > PAGE_SIZE && (
            <div className="flex items-center justify-center mt-8">
              <button
                onClick={() => { setShowAll(false); setPage(1) }}
                className="h-9 px-4 flex items-center justify-center rounded-xl border border-gray-200 text-sm font-bold text-gray-600 hover:border-brand hover:text-brand transition-colors"
              >
                Collapse
              </button>
            </div>
          )}

          {!showAll && totalPages > 1 && (
            <div className="flex flex-wrap items-center justify-center gap-1 mt-8">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="w-9 h-9 flex items-center justify-center rounded-xl border border-gray-200 text-gray-500 hover:border-brand hover:text-brand disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight size={16} className="rotate-180" />
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .filter(p => p === 1 || p === totalPages || Math.abs(p - page) <= 2)
                .reduce<(number | '...')[]>((acc, p, i, arr) => {
                  if (i > 0 && p - (arr[i - 1] as number) > 1) acc.push('...')
                  acc.push(p)
                  return acc
                }, [])
                .map((p, i) =>
                  p === '...' ? (
                    <span key={`dots-${i}`} className="w-9 h-9 flex items-center justify-center text-gray-400">…</span>
                  ) : (
                    <button
                      key={p}
                      onClick={() => setPage(p)}
                      className={`w-9 h-9 flex items-center justify-center rounded-xl text-sm font-bold leading-none pt-px transition-colors ${
                        page === p ? 'bg-brand text-white' : 'border border-gray-200 text-gray-600 hover:border-brand hover:text-brand'
                      }`}
                    >
                      {p}
                    </button>
                  )
                )}
              <button
                onClick={() => setPage(p => p + 1)}
                disabled={page === totalPages}
                className="w-9 h-9 flex items-center justify-center rounded-xl border border-gray-200 text-gray-500 hover:border-brand hover:text-brand disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight size={16} />
              </button>
              <button
                onClick={() => setShowAll(true)}
                className="ml-2 h-9 px-3 flex items-center justify-center rounded-xl border border-gray-200 text-sm font-bold text-gray-600 hover:border-brand hover:text-brand transition-colors"
              >
                Show all ({data.count})
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
