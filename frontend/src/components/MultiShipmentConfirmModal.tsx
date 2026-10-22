import { Truck } from 'lucide-react'
import { useCart } from '../context/CartContext'
import { Modal, Button } from './ui'

export default function MultiShipmentConfirmModal() {
  const { pendingConfirm, confirmPending, cancelPending } = useCart()
  const open = !!pendingConfirm

  return (
    <Modal open={open} onClose={cancelPending} ariaLabel="Multiple parcels" size="md">
      {pendingConfirm && (
        <>
          <div className="flex items-center gap-3 mb-4">
            <div className="w-11 h-11 shrink-0 flex items-center justify-center rounded-xl bg-amber-50">
              <Truck size={22} className="text-warning" />
            </div>
            <h2 className="text-lg font-bold text-text-primary">Multiple parcels</h2>
          </div>

          <p className="text-sm text-text-primary leading-relaxed mb-5">
            The item <span className="font-bold">“{pendingConfirm.item.productName}”</span> ships separately
            from what is already in your cart. Your order will be split into <span className="font-bold">{pendingConfirm.totalShipments} parcels</span>.
          </p>

          <div className="flex flex-col-reverse sm:flex-row gap-2">
            <Button variant="secondary" size="md" fullWidth onClick={cancelPending}>
              Cancel
            </Button>
            <Button variant="primary" size="md" fullWidth onClick={confirmPending}>
              Add anyway
            </Button>
          </div>
        </>
      )}
    </Modal>
  )
}
