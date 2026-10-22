import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Card } from '../../components/ui'

export default function PrivacyPage() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      <nav className="flex items-center gap-1 text-xs sm:text-sm text-text-muted mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-text-secondary">Privacy Policy</span>
      </nav>

      <Card variant="section" className="p-5 sm:p-8 space-y-5 sm:space-y-6">

        <div>
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-800 mb-2">Privacy Policy</h1>
          <p className="text-sm text-gray-400">Last updated: June 27, 2026</p>
        </div>

        <p className="text-gray-600 leading-relaxed">
          This Personal Data Privacy Policy (hereinafter — the Privacy Policy) applies to all information
          that the online store “Magiya Roslyn”, located on this website and operated by the sole proprietor [seller's full name]
          (hereinafter — the Administration), may obtain about the User while the site is being used.
        </p>

        {/* 1 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">1. Definitions</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p><span className="font-semibold">“Personal data”</span> — any information relating directly or indirectly to an identified or identifiable natural person (the data subject).</p>
            <p><span className="font-semibold">“Processing of personal data”</span> — any action or set of actions performed on personal data: collection, recording, storage, rectification, use, transfer, deletion or destruction.</p>
            <p><span className="font-semibold">“User”</span> — a person who uses the online store's website.</p>
          </div>
        </section>

        {/* 2 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">2. General provisions</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>2.1. By using the site, the User agrees to this Privacy Policy and to the terms of personal data processing.</p>
            <p>2.2. If the User does not agree with the terms of the Privacy Policy, the User must stop using the site.</p>
            <p>2.3. The Administration does not verify the accuracy of the personal data provided by the User.</p>
          </div>
        </section>

        {/* 3 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">3. Personal data covered</h2>
          <p className="text-gray-600 leading-relaxed mb-2">
            The personal data processed under this Policy is provided by the User when registering or placing an order and includes:
          </p>
          <ul className="list-disc list-inside space-y-1 text-gray-600 ml-2">
            <li>last name, first name and patronymic;</li>
            <li>contact phone number;</li>
            <li>e-mail address;</li>
            <li>delivery address (a Nova Poshta branch).</li>
          </ul>
          <p className="text-gray-600 leading-relaxed mt-2">
            The site also automatically receives technical data: the IP address, browser type and the time pages were visited. To analyze
            traffic and improve the site, we use cookies and the web analytics services Google Analytics
            (Google LLC) and Meta Pixel (Meta Platforms, Inc.). They collect anonymized data about actions on the site (page and
            product views, additions to the cart, orders placed) for statistics and for showing relevant advertising. This information does not
            identify a person. The User can disable cookies in the browser settings or opt out of personalized
            advertising in the settings of the respective services.
          </p>
        </section>

        {/* 4 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">4. Purposes of collecting personal data</h2>
          <p className="text-gray-600 leading-relaxed mb-2">The Administration uses the User's personal data in order to:</p>
          <ul className="list-disc list-inside space-y-1 text-gray-600 ml-2">
            <li>identify the User for placing and fulfilling an order;</li>
            <li>contact the User about the status of an order;</li>
            <li>deliver goods through a delivery service;</li>
            <li>provide customer support;</li>
            <li>comply with the requirements of the legislation of Ukraine.</li>
          </ul>
        </section>

        {/* 5 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">5. Methods and periods of processing personal data</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>5.1. Personal data is processed by any lawful means, with or without the use of automation tools.</p>
            <p>5.2. The User's personal data may be transferred to third parties — the delivery service (Nova Poshta) — solely for the purpose of fulfilling the order.</p>
            <p>5.3. Personal data may be transferred to the authorized public authorities of Ukraine only on the grounds and in the manner established by the legislation of Ukraine.</p>
            <p>5.4. Data is stored for the period necessary to fulfil the order and to resolve possible disputes, after which it is deleted or anonymized.</p>
            <p>5.5. In the event of loss or disclosure of personal data, the Administration undertakes to notify the User.</p>
          </div>
        </section>

        {/* 6 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">6. User rights</h2>
          <p className="text-gray-600 leading-relaxed mb-2">In accordance with the Law of Ukraine “On Personal Data Protection”, the User has the right to:</p>
          <ul className="list-disc list-inside space-y-1 text-gray-600 ml-2">
            <li>know about the sources of collection, the location and the purpose of processing of their personal data;</li>
            <li>receive information about the conditions of granting access to personal data;</li>
            <li>demand correction or deletion of their personal data;</li>
            <li>withdraw consent to the processing of personal data.</li>
          </ul>
          <p className="text-gray-600 leading-relaxed mt-2">
            To exercise these rights, the User may contact the Administration at:{' '}
            <a href="mailto:shop@example.com" className="text-brand hover:underline">shop@example.com</a>.
          </p>
        </section>

        {/* 7 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">7. Obligations of the Administration</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>7.1. To use the information received solely for the purposes specified in section 4 of this Policy.</p>
            <p>7.2. To keep personal data confidential and not disclose it without the User's prior written permission, except in the cases provided for in clauses 5.2 and 5.3.</p>
            <p>7.3. To take organizational and technical measures to protect personal data from unlawful access, destruction or disclosure.</p>
          </div>
        </section>

        {/* 8 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">8. Liability</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>8.1. The Administration is liable for losses incurred by the User in connection with the unlawful use of personal data, in accordance with the legislation of Ukraine.</p>
            <p>8.2. The Administration is not liable if the confidential information became public knowledge before its loss or disclosure, or was disclosed with the User's consent.</p>
          </div>
        </section>

        {/* 9 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">9. Dispute resolution</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>9.1. Before going to court, pre-trial settlement of the dispute by sending a written claim is mandatory.</p>
            <p>9.2. The recipient of the claim must notify the other party in writing of the results of its review within 30 calendar days.</p>
            <p>9.3. If no agreement is reached, the dispute is referred to a court in accordance with the applicable legislation of Ukraine.</p>
          </div>
        </section>

        {/* 10 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">10. Additional terms</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>10.1. The Administration has the right to amend this Privacy Policy without prior notice to the User.</p>
            <p>10.2. The new version of the Policy takes effect from the moment it is posted on the site.</p>
          </div>
        </section>

        <div className="border-t border-border pt-4 text-sm text-text-muted">
          <p>Sole proprietor [seller's full name]</p>
          <p>Email: <a href="mailto:shop@example.com" className="text-brand hover:underline">shop@example.com</a></p>
        </div>

      </Card>
    </div>
  )
}
