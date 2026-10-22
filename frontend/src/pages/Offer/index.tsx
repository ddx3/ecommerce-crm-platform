import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Card } from '../../components/ui'

export default function OfferPage() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      <nav className="flex items-center gap-1 text-xs sm:text-sm text-text-muted mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-text-secondary">Public offer</span>
      </nav>

      <Card variant="section" className="p-5 sm:p-8 space-y-5 sm:space-y-6">

        <div>
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-800 mb-2">Public offer agreement</h1>
          <p className="text-sm text-gray-400">Last updated: April 6, 2025</p>
        </div>

        <p className="text-gray-600 leading-relaxed">
          Sole proprietor [seller's full name] (hereinafter the Seller), acting in accordance with Article 633 of the Civil Code of Ukraine,
          offers an unlimited number of individuals (hereinafter the Buyer) to conclude a contract for the distance sale of goods
          on the terms set out below.
        </p>

        {/* 1 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">1. General provisions</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>1.1. This public offer is an official proposal by the Seller to conclude a contract for the sale of the goods presented on the website of the online store «Magiya Roslyn».</p>
            <p>1.2. Placing an order by the Buyer constitutes unconditional acceptance of the terms of this Agreement and is equivalent to signing the agreement.</p>
            <p>1.3. The Seller reserves the right to amend this Agreement without prior notice. The current version is always published on the website.</p>
          </div>
        </section>

        {/* 2 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">2. Subject of the agreement</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>2.1. The Seller undertakes to transfer to the Buyer the ownership of the goods selected on the website, and the Buyer undertakes to accept and pay for them on the terms of this Agreement.</p>
            <p>2.2. Ownership of the goods passes to the Buyer upon actual receipt and full payment.</p>
          </div>
        </section>

        {/* 3 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">3. Placing an order</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>3.1. The Buyer places the order independently on the website or through a manager.</p>
            <p>3.2. After the order is placed, the Seller confirms the availability of the goods and sends the Buyer a confirmation to the specified email or phone.</p>
            <p>3.3. The Seller has the right to refuse to fulfil the order if the goods are out of stock, informing the Buyer accordingly.</p>
          </div>
        </section>

        {/* 4 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">4. Price and payment</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>4.1. Prices of the goods are stated in hryvnias (UAH) and include all taxes and fees required by law.</p>
            <p>4.2. The Seller reserves the right to change the prices of the goods at any time. The price fixed at the time of placing the order does not change.</p>
            <p>4.3. Payment is made by one of the available methods: online by card via Monobank, or by transfer to a PrivatBank or Monobank card.</p>
            <p>4.4. The order is fulfilled after 100% prepayment is received.</p>
            <p>4.5. The minimum order amount is 250 UAH.</p>
          </div>
        </section>

        {/* 5 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">5. Delivery</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>5.1. Delivery is carried out by the Nova Poshta service throughout Ukraine.</p>
            <p>5.2. The delivery cost is paid by the Buyer in accordance with the delivery service's rates.</p>
            <p>5.3. Delivery time: 1–3 business days after dispatch. Dispatch usually takes place the day after payment is confirmed.</p>
            <p>5.4. The risk of accidental damage to or loss of the goods passes to the Buyer from the moment the goods are handed over to the delivery service.</p>
          </div>
        </section>

        {/* 6 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">6. Returns and exchanges</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>6.1. Returns and exchanges of goods are carried out in accordance with the Law of Ukraine "On Consumer Rights Protection".</p>
            <p>6.2. The Buyer has the right to return goods of proper quality within <span className="font-semibold">14 calendar days</span> of receipt, provided the goods have not been used and their presentation, consumer properties and original packaging are preserved.</p>
            <p>6.3. Goods of improper quality are returned within the warranty period. The Seller undertakes to replace the goods or refund the money within 7 business days of receiving the return.</p>
            <p>6.4. The delivery costs for returning goods of proper quality are borne by the Buyer. For returning goods of improper quality — by the Seller.</p>
            <p>6.5. To initiate a return, the Buyer contacts the Seller at <a href="mailto:shop@example.com" className="text-brand hover:underline">shop@example.com</a>.</p>
          </div>
        </section>

        {/* 7 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">7. Rights and obligations of the parties</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p className="font-semibold">The Seller undertakes to:</p>
            <ul className="list-disc list-inside ml-2 space-y-1">
              <li>deliver goods of proper quality to the Buyer within the agreed time;</li>
              <li>provide complete and reliable information about the goods;</li>
              <li>keep the Buyer's personal data confidential in accordance with the Privacy Policy.</li>
            </ul>
            <p className="font-semibold mt-2">The Buyer undertakes to:</p>
            <ul className="list-disc list-inside ml-2 space-y-1">
              <li>provide reliable data when placing the order;</li>
              <li>pay for and accept the ordered goods on time;</li>
              <li>read the terms of this Agreement before placing the order.</li>
            </ul>
          </div>
        </section>

        {/* 8 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">8. Liability of the parties</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>8.1. The parties are liable for non-performance or improper performance of the terms of this Agreement in accordance with the applicable legislation of Ukraine.</p>
            <p>8.2. The Seller is not liable for a delivery delay caused by the delivery service or by force majeure circumstances.</p>
            <p>8.3. The Seller is not liable if the goods do not meet the Buyer's expectations, provided the goods correspond to the description on the website.</p>
          </div>
        </section>

        {/* 9 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">9. Dispute resolution</h2>
          <div className="space-y-2 text-gray-600 leading-relaxed">
            <p>9.1. All disputes arising in connection with the performance of this Agreement are resolved through negotiations.</p>
            <p>9.2. If no agreement is reached, the dispute is referred to a court in accordance with the applicable legislation of Ukraine.</p>
          </div>
        </section>

        {/* 10 */}
        <section>
          <h2 className="text-lg sm:text-xl font-bold text-gray-800 mb-3">10. Force majeure</h2>
          <p className="text-gray-600 leading-relaxed">
            The parties are released from liability for non-performance of obligations in the event of force majeure circumstances
            (natural disasters, military actions, decisions of government authorities, etc.), if such circumstances directly affected
            the performance of the Agreement. The party affected by force majeure must notify the other party as soon as possible.
          </p>
        </section>

        <div className="border-t border-border pt-4 text-sm text-text-muted">
          <p className="font-semibold text-text-secondary mb-1">Seller's details</p>
          <p>Sole proprietor [seller's full name]</p>
          <p>Email: <a href="mailto:shop@example.com" className="text-brand hover:underline">shop@example.com</a></p>
        </div>

      </Card>
    </div>
  )
}
