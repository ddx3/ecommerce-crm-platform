import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ChevronRight, ChevronLeft, Calendar } from 'lucide-react'
import { getBlogPost } from '../../api/blog'
import type { BlogPostDetail } from '../../types'
import { sanitizeHtml } from '../../utils/sanitize'
import Seo from '../../components/Seo'

function formatDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString('uk-UA', { day: 'numeric', month: 'long', year: 'numeric' })
}

function extractYouTubeId(url: string): string | null {
  if (!url) return null
  const m = url.match(/(?:youtube\.com\/(?:watch\?v=|embed\/|v\/)|youtu\.be\/)([A-Za-z0-9_-]{6,})/)
  return m ? m[1] : null
}

export default function BlogPostPage() {
  const { slug } = useParams<{ slug: string }>()
  const [post, setPost] = useState<BlogPostDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  useEffect(() => {
    if (!slug) return
    setLoading(true)
    setError(false)
    getBlogPost(slug)
      .then(setPost)
      .catch(() => setError(true))
      .finally(() => setLoading(false))
  }, [slug])

  if (loading) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        <div className="w-10 h-10 border-4 border-gray-200 border-t-brand rounded-full animate-spin" />
      </div>
    )
  }

  if (error || !post) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 text-center">
        <h1 className="text-2xl font-bold text-gray-900 mb-2">Article not found</h1>
        <Link to="/blog" className="text-brand hover:underline inline-flex items-center gap-1"><ChevronLeft size={18} /> All articles</Link>
      </div>
    )
  }

  const youtubeId = extractYouTubeId(post.youtube_url)

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <Seo
        title={post.meta_title || post.title}
        description={post.meta_description || post.excerpt}
        canonicalPath={`/blog/${post.slug}`}
        image={post.preview_image}
        ogType="article"
      />
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <Link to="/blog" className="hover:text-brand transition-colors">Blog</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600 line-clamp-1">{post.title}</span>
      </nav>

      <article>
        <h1 className="text-2xl sm:text-4xl font-bold text-gray-900 mb-3 sm:mb-4">{post.title}</h1>
        <div className="flex items-center gap-2 text-sm text-gray-400 mb-6 sm:mb-8">
          <Calendar size={14} />
          <span>{formatDate(post.created_at)}</span>
        </div>

        {post.content && (
          <div
            className="prose text-base sm:text-lg text-gray-800 leading-relaxed mb-8"
            dangerouslySetInnerHTML={{ __html: sanitizeHtml(post.content) }}
          />
        )}

        {youtubeId && (
          <div className="mb-8">
            <div className="aspect-video rounded-2xl overflow-hidden bg-black">
              <iframe
                className="w-full h-full"
                src={`https://www.youtube.com/embed/${youtubeId}`}
                title="YouTube"
                frameBorder={0}
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                allowFullScreen
              />
            </div>
          </div>
        )}
      </article>
    </div>
  )
}
