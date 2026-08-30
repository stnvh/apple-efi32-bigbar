// SPDX-License-Identifier: GPL-2.0
/*
 * bigbar - assign oversized PCI BARs above 4GB on firmware that couldn't.
 *
 * Some firmware (e.g. 32-bit EFI Macs) cannot allocate very large PCI BARs
 * such as the 32GB BAR1 on an NVIDIA Tesla P40, and leaves them unassigned.
 * Linux often cannot fix this either: a 32GB BAR needs 32GB alignment, and
 * once the parent bridge window must also hold the device's other
 * prefetchable BARs the total no longer fits under the CPU's physical
 * address limit.
 *
 * This module finds unassigned memory BARs at or above min_size, places the
 * largest one at the top of the physical address space (size-aligned),
 * reprograms the parent bridge's prefetchable window to match, writes the
 * BAR, and fixes up the kernel's resource tree so drivers see it.
 *
 * Nothing is persistent - a reboot restores the previous state.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/pci.h>
#include <linux/ioport.h>

static unsigned long long min_size = 256ULL << 20;   /* 256MB */
module_param(min_size, ullong, 0444);
MODULE_PARM_DESC(min_size, "only consider unassigned BARs >= this many bytes");

static bool dry_run;
module_param(dry_run, bool, 0444);
MODULE_PARM_DESC(dry_run, "report what would be done, change nothing");

static unsigned int phys_bits = 0;
module_param(phys_bits, uint, 0444);
MODULE_PARM_DESC(phys_bits, "override CPU physical address bits (0 = detect)");

/* remember what we touched so we can be polite on unload */
struct bigbar_fix {
	struct pci_dev *dev;
	int bar;
	struct resource saved;
};
static struct bigbar_fix fixes[8];
static int nr_fixes;

static u64 phys_limit(void)
{
	unsigned int bits = phys_bits ? phys_bits : boot_cpu_data.x86_phys_bits;

	if (bits < 32 || bits > 52)
		bits = 36;
	return (1ULL << bits) - 1;
}

/*
 * Program a bridge's 64-bit prefetchable window.  Order matters: the window
 * must never be momentarily active across a range that contains RAM, or the
 * bridge will start claiming memory cycles and the machine dies instantly.
 * So we park it disabled (base > limit), set the upper dwords, then the
 * base, and only enable it by writing the limit last.
 */
static void bridge_set_pref_window(struct pci_dev *br, u64 base, u64 limit)
{
	u16 b_lo = ((base >> 20) & 0xfff0) | 0x1;   /* bit0: 64-bit capable */
	u16 l_lo = ((limit >> 20) & 0xfff0) | 0x1;

	/* park disabled: base = max, limit = 0 */
	pci_write_config_word(br, PCI_PREF_MEMORY_BASE, 0xfff1);
	pci_write_config_word(br, PCI_PREF_MEMORY_LIMIT, 0x0001);

	pci_write_config_dword(br, PCI_PREF_BASE_UPPER32, upper_32_bits(base));
	pci_write_config_dword(br, PCI_PREF_LIMIT_UPPER32, upper_32_bits(limit));

	pci_write_config_word(br, PCI_PREF_MEMORY_BASE, b_lo);
	pci_write_config_word(br, PCI_PREF_MEMORY_LIMIT, l_lo);   /* enables */
}

static void bar_write(struct pci_dev *dev, int bar, u64 addr)
{
	int reg = PCI_BASE_ADDRESS_0 + bar * 4;

	pci_write_config_dword(dev, reg, lower_32_bits(addr));
	pci_write_config_dword(dev, reg + 4, upper_32_bits(addr));
}

static unsigned long long low_base = 0x90000000ULL;
module_param(low_base, ullong, 0444);
MODULE_PARM_DESC(low_base, "base for the rebuilt sub-4GB bridge window");

static u64 probe_bar(struct pci_dev *dev, int bar, u64 *cur, bool *is64);

/*
 * Linux's decode_bar() seeds a resource's flags with the BAR's own low nibble
 * (type + prefetch bits) before OR-ing in the IORESOURCE_* values, so real
 * resources carry both.  Drivers rely on it: NVIDIA's
 * nv_bar_index_to_os_bar_index() tests flags & PCI_BASE_ADDRESS_MEM_TYPE_64
 * (0x4) to work out whether a BAR eats one PCI slot or two.  Build flags from
 * IORESOURCE_* alone and a 64-bit BAR looks 32-bit, so the driver indexes the
 * wrong resource and reports the empty high half as "0M @ 0x0".
 */
static inline unsigned long bar_type_bits(u32 bar_lo)
{
	return bar_lo & ~PCI_BASE_ADDRESS_MEM_MASK;
}

/*
 * Moving the prefetchable window up to hold the huge BAR orphans every other
 * memory BAR behind that bridge, because a bridge has only one prefetchable
 * window and the big BAR fills it completely.  Those BARs have to be rehomed
 * into the NON-prefetchable window instead (legal: a prefetchable BAR may sit
 * in a non-prefetchable window, it just loses the prefetch hint).  That window
 * is 32-bit only, so everything lands below 4GB.
 *
 * BAR0 is relocated too - the window must contain all of them, and leaving it
 * where it is would force a window so wide it overlaps neighbouring bridges.
 */
static void rehome_low_bars(struct pci_dev *dev, int big_bar)
{
	struct pci_dev *br = dev->bus->self;
	struct resource *mw;
	u64 addr = low_base;
	int bar, order;
	u16 cmd, b_lo, l_lo;

	pci_read_config_word(dev, PCI_COMMAND, &cmd);
	pci_write_config_word(dev, PCI_COMMAND, cmd & ~PCI_COMMAND_MEMORY);

	/* largest first so natural alignment packs without gaps */
	for (order = 31; order >= 12; order--) {
		for (bar = 0; bar < PCI_STD_NUM_BARS; bar++) {
			struct resource *r = &dev->resource[bar];
			u64 size, cur = 0;
			bool is64 = false;

			if (bar == big_bar)
				continue;
			size = probe_bar(dev, bar, &cur, &is64);
			if (!size || size != (1ULL << order))
				continue;
			if (addr + size - 1 >= 0x100000000ULL) {
				pci_info(dev, "bigbar: no room under 4GB for BAR%d\n",
					 bar);
				continue;
			}

			addr = ALIGN(addr, size);
			bar_write(dev, bar, addr);

			if (r->parent)
				release_resource(r);
			r->start = addr;
			r->end = addr + size - 1;
			/*
			 * Keep the prefetchable bit consistent with what the
			 * hardware BAR actually reports - a resource that
			 * disagrees with config space upsets driver checks.
			 */
			{
				u32 lo;

				pci_read_config_dword(dev,
					PCI_BASE_ADDRESS_0 + bar * 4, &lo);
				r->flags = bar_type_bits(lo) |
					IORESOURCE_MEM |
					(is64 ? IORESOURCE_MEM_64 : 0) |
					((lo & PCI_BASE_ADDRESS_MEM_PREFETCH) ?
					 IORESOURCE_PREFETCH : 0);
			}
			r->parent = NULL;
			pci_info(dev, "bigbar: BAR%d %llu MB -> 0x%llx\n",
				 bar, size >> 20, addr);
			addr += size;
		}
	}

	/* bridge non-prefetchable window, 1MB granular */
	b_lo = (low_base >> 16) & 0xfff0;
	l_lo = ((addr - 1) >> 16) & 0xfff0;
	pci_write_config_word(br, PCI_MEMORY_BASE, b_lo);
	pci_write_config_word(br, PCI_MEMORY_LIMIT, l_lo);
	pci_info(dev, "bigbar: bridge %s mem window -> 0x%llx-0x%llx\n",
		 pci_name(br), low_base, addr - 1);

	mw = dev->bus->resource[1];
	if (mw) {
		if (mw->parent)
			release_resource(mw);
		mw->start = low_base;
		mw->end = addr - 1;
		mw->flags = IORESOURCE_MEM;
		if (insert_resource(&iomem_resource, mw))
			pci_info(dev, "bigbar: mem window not insertable\n");
	}

	for (bar = 0; bar < PCI_STD_NUM_BARS; bar++) {
		struct resource *r = &dev->resource[bar];

		if (bar == big_bar || !r->start || r->parent)
			continue;
		if (r->flags & IORESOURCE_MEM)
			pci_claim_resource(dev, bar);
	}

	pci_write_config_word(dev, PCI_COMMAND, cmd);
}

static int fixup_one(struct pci_dev *dev, int bar)
{
	struct resource *r = &dev->resource[bar];
	struct pci_dev *br = dev->bus ? dev->bus->self : NULL;
	struct resource *bw;
	resource_size_t size = resource_size(r);
	u64 limit = phys_limit();
	u64 base;
	u16 cmd;

	if (!br) {
		pci_info(dev, "bigbar: no parent bridge, skipping BAR%d\n", bar);
		return -ENODEV;
	}

	/* highest size-aligned base that still fits under the phys limit */
	base = (limit + 1 - size) & ~(u64)(size - 1);
	if (base < (4ULL << 30) || base + size - 1 > limit) {
		pci_info(dev, "bigbar: BAR%d (%llu MB) will not fit under 0x%llx\n",
			 bar, (unsigned long long)size >> 20, limit);
		return -ENOSPC;
	}

	pci_info(dev, "bigbar: BAR%d size %llu MB -> 0x%llx-0x%llx (bridge %s)\n",
		 bar, (unsigned long long)size >> 20, base, base + size - 1,
		 pci_name(br));

	if (dry_run)
		return 0;

	if (nr_fixes >= ARRAY_SIZE(fixes))
		return -ENOMEM;
	fixes[nr_fixes].dev = pci_dev_get(dev);
	fixes[nr_fixes].bar = bar;
	fixes[nr_fixes].saved = *r;
	nr_fixes++;

	/* stop the device decoding while its BAR moves */
	pci_read_config_word(dev, PCI_COMMAND, &cmd);
	pci_write_config_word(dev, PCI_COMMAND,
			      cmd & ~(PCI_COMMAND_MEMORY | PCI_COMMAND_MASTER));

	bridge_set_pref_window(br, base, base + size - 1);
	bar_write(dev, bar, base);

	/* teach the kernel about the bridge window ... */
	bw = dev->bus->resource[2];   /* array of pointers, not structs */
	if (bw) {
		bw->start = base;
		bw->end = base + size - 1;
		bw->flags = IORESOURCE_MEM | IORESOURCE_PREFETCH |
			    IORESOURCE_MEM_64;
	} else {
		pci_info(dev, "bigbar: bus has no pref window resource\n");
	}

	/* ... and about the BAR itself, so pci_resource_start() works */
	r->start = base;
	r->end = base + size - 1;
	r->flags &= ~IORESOURCE_UNSET;
	r->parent = NULL;

	/*
	 * Filling in start/end is not enough: pci_enable_device() refuses a
	 * BAR whose resource has no parent ("not claimed"), so the resource
	 * has to be inserted into the tree.  Put the bridge window in first
	 * if it was released, then claim the BAR beneath it.
	 */
	if (bw && !bw->parent) {
		if (insert_resource(&iomem_resource, bw))
			pci_info(dev, "bigbar: bridge window not insertable\n");
	}

	if (pci_claim_resource(dev, bar)) {
		/* no usable parent window - fall back to the global tree */
		if (insert_resource(&iomem_resource, r))
			pci_info(dev, "bigbar: BAR%d could not be claimed\n",
				 bar);
		else
			pci_info(dev, "bigbar: BAR%d claimed via iomem\n", bar);
	} else {
		pci_info(dev, "bigbar: BAR%d claimed under %s\n", bar,
			 r->parent ? r->parent->name : "?");
	}

	pci_write_config_word(dev, PCI_COMMAND,
			      cmd | PCI_COMMAND_MEMORY | PCI_COMMAND_MASTER);

	pci_info(dev, "bigbar: BAR%d now at %pR\n", bar, r);

	/* the big BAR consumed the whole pref window - rehome the rest */
	rehome_low_bars(dev, bar);

	pci_write_config_word(dev, PCI_COMMAND,
			      cmd | PCI_COMMAND_MEMORY | PCI_COMMAND_MASTER);

	/*
	 * The kernel snapshotted this device's config space before we touched
	 * it.  Any later reset - a failed driver probe calling
	 * pci_restore_state(), resume, FLR - would write those firmware BAR
	 * values straight back, silently undoing everything above while the
	 * bridge keeps our windows.  The device then decodes nothing and looks
	 * like it has "fallen off the bus".  Re-snapshot so our layout is what
	 * gets restored.
	 */
	pci_save_state(dev);
	pci_info(dev, "bigbar: config state re-saved for restore\n");
	return 0;
}

/*
 * When the kernel fails to assign a BAR it zeroes the whole resource -
 * start, end AND flags - so dev->resource[] tells us nothing.  Size the
 * BAR straight from config space instead, the standard way: write all
 * ones, read back the mask, restore.  Safe here because we only do this
 * for devices with no driver bound, and with memory decode disabled.
 */
static u64 probe_bar(struct pci_dev *dev, int bar, u64 *cur, bool *is64)
{
	int reg = PCI_BASE_ADDRESS_0 + bar * 4;
	u32 lo, hi = 0, mask_lo, mask_hi = 0;
	u64 size;
	u16 cmd;

	pci_read_config_dword(dev, reg, &lo);
	if (lo & PCI_BASE_ADDRESS_SPACE_IO)
		return 0;

	*is64 = (lo & PCI_BASE_ADDRESS_MEM_TYPE_MASK) ==
		PCI_BASE_ADDRESS_MEM_TYPE_64;
	if (*is64)
		pci_read_config_dword(dev, reg + 4, &hi);

	*cur = ((u64)hi << 32) | (lo & PCI_BASE_ADDRESS_MEM_MASK);

	pci_read_config_word(dev, PCI_COMMAND, &cmd);
	pci_write_config_word(dev, PCI_COMMAND, cmd & ~PCI_COMMAND_MEMORY);

	pci_write_config_dword(dev, reg, ~0);
	pci_read_config_dword(dev, reg, &mask_lo);
	pci_write_config_dword(dev, reg, lo);
	if (*is64) {
		pci_write_config_dword(dev, reg + 4, ~0);
		pci_read_config_dword(dev, reg + 4, &mask_hi);
		pci_write_config_dword(dev, reg + 4, hi);
	}

	pci_write_config_word(dev, PCI_COMMAND, cmd);

	/*
	 * Complement in the BAR's own width.  Doing a 32-bit BAR's mask in
	 * 64-bit space turns an unimplemented BAR into an absurd size.
	 */
	mask_lo &= PCI_BASE_ADDRESS_MEM_MASK;
	if (*is64) {
		size = ((u64)mask_hi << 32) | mask_lo;
		if (!size)
			return 0;
		size = ~size + 1;
	} else {
		if (!mask_lo)
			return 0;
		size = (u32)(~mask_lo) + 1;
	}

	/* a real BAR size is always a power of two */
	if (!size || (size & (size - 1)))
		return 0;
	return size;
}

static int __init bigbar_init(void)
{
	struct pci_dev *dev = NULL;
	int found = 0, bar;

	pr_info("bigbar: scanning for unassigned MEM BARs >= %llu MB (phys limit 0x%llx)%s\n",
		min_size >> 20, phys_limit(), dry_run ? " [dry run]" : "");

	for_each_pci_dev(dev) {
		/*
		 * ONLY normal endpoints.  On a Type 1 (bridge) header the
		 * registers above 0x14 are bus numbers and windows, not BARs -
		 * "sizing" them writes 0xFFFFFFFF into the secondary and
		 * subordinate bus numbers and scrambles the bridge's windows,
		 * which wedges the machine.  Bridges are never what we want
		 * anyway; the oversized BAR is always on an endpoint.
		 */
		if (dev->hdr_type != PCI_HEADER_TYPE_NORMAL) {
			pci_dbg(dev, "bigbar: not an endpoint, skipping\n");
			continue;
		}

		/* never poke a device someone is already driving */
		if (dev->driver) {
			pci_dbg(dev, "bigbar: driver bound, skipping\n");
			continue;
		}

		for (bar = 0; bar < PCI_STD_NUM_BARS; bar++) {
			struct resource *r = &dev->resource[bar];
			u64 size, cur = 0;
			bool is64 = false;

			/* kernel already has it placed?  leave alone */
			if ((r->flags & IORESOURCE_MEM) && r->start &&
			    !(r->flags & IORESOURCE_UNSET))
				continue;

			size = probe_bar(dev, bar, &cur, &is64);
			if (size < min_size)
				continue;

			pci_info(dev,
				 "bigbar: BAR%d %llu MB%s, hw=0x%llx, kernel resource %s\n",
				 bar, size >> 20, is64 ? " 64-bit" : "", cur,
				 r->flags ? "partial" : "EMPTY");

			found++;

			if (dry_run) {
				pci_info(dev,
					 "bigbar: [dry run] would place BAR%d at 0x%llx\n",
					 bar, (unsigned long long)
					 (((phys_limit() + 1 - size) &
					   ~(u64)(size - 1))));
				if (is64)
					bar++;
				continue;
			}

			/* record the size the kernel lost, then place it.
			 * Low nibble from the BAR itself - see bar_type_bits().
			 */
			{
				u32 lo;

				pci_read_config_dword(dev,
					PCI_BASE_ADDRESS_0 + bar * 4, &lo);
				r->flags = bar_type_bits(lo) | IORESOURCE_MEM |
					   IORESOURCE_MEM_64 |
					   ((lo & PCI_BASE_ADDRESS_MEM_PREFETCH) ?
					    IORESOURCE_PREFETCH : 0) |
					   IORESOURCE_UNSET;
			}
			r->start = 0;
			r->end = size - 1;

			fixup_one(dev, bar);

			if (is64)
				bar++;   /* 64-bit BAR eats the next slot */
		}
	}

	if (!found)
		pr_info("bigbar: no unassigned large BARs found\n");
	return 0;
}

static void __exit bigbar_exit(void)
{
	int i;

	for (i = 0; i < nr_fixes; i++) {
		struct resource *r = &fixes[i].dev->resource[fixes[i].bar];

		pci_info(fixes[i].dev, "bigbar: releasing BAR%d fixup\n",
			 fixes[i].bar);
		/* must leave the resource tree as we found it */
		if (r->parent)
			release_resource(r);
		*r = fixes[i].saved;
		pci_dev_put(fixes[i].dev);
	}
	pr_info("bigbar: unloaded (hardware left as-is until reboot)\n");
}

module_init(bigbar_init);
module_exit(bigbar_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Assign oversized PCI BARs above 4GB when firmware cannot");
